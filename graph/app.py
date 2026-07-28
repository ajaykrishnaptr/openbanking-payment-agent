"""The payment agent graph.

Route:
    check_input
      -> need_more_info                     (something required is missing)
      -> verify_payee
           -> hold_or_reject                (NO_MATCH / MATCH_NOT_POSSIBLE)
           -> check_consent
                -> hold_or_reject           (expired / missing)
                -> assess_risk
                     -> execute_payment     (clean MATCH, under ceiling, no flags)
                     -> human_approval      (anything else)
                          -> execute_payment    (approved)
                          -> hold_or_reject     (denied)
    execute_payment -> reconcile -> END

What is real and what is not:
  real       OAuth, request signing, POST /v3/payments, GET payment status
  simulated  Verification of Payee (see vop.py), consent records, payee history
  sandbox    every payment. No real money moves, ever.
"""

from __future__ import annotations

import os
import uuid
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from . import policy
from .vop import get_vop_adapter

# "dry" keeps the graph off the network, which is what the evals run against.
# "live" posts a real payment to the TrueLayer sandbox.
PAYMENTS_MODE = os.getenv("PAYMENTS_MODE", "dry")
# Where TrueLayer sends the payer after they authenticate at the bank. It must
# be a route this app serves AND be allow-listed in the TrueLayer console, or
# the payment is rejected with 400 "Return URI must be added in the Console".
# The HTTP layer passes the live origin per request, so the same code is right
# on localhost, on a preview URL and in production. This is only the fallback.
RETURN_URI = os.getenv("RETURN_URI", "https://openbanking-payment-agent.vercel.app/callback")


class State(TypedDict, total=False):
    # input
    user_id: str
    payee_name: str
    account: dict          # {"sort_code": ..., "account_number": ...}
    amount_minor: int
    currency: str
    reference: str
    return_uri: str
    # working
    missing: list[str]
    vop: dict
    consent: dict
    risk_flags: list[str]
    human_decision: str
    execution: dict
    outcome: str
    trail: list[str]


def _step(state: State, name: str) -> list[str]:
    return state.get("trail", []) + [name]


# ---------------------------------------------------------------- input

# The rail has limits of its own. TrueLayer rejects a reference over 18
# characters with a 400, so checking here turns a wasted API call and a
# confusing provider error into a plain question at the front door.
REFERENCE_MAX = 18


def check_input(state: State) -> dict:
    required = ("payee_name", "account", "amount_minor")
    missing = [f for f in required if not state.get(f)]

    reference = state.get("reference") or ""
    if len(reference) > REFERENCE_MAX:
        missing.append(
            f"a shorter reference (the bank allows {REFERENCE_MAX} characters, this one has {len(reference)})"
        )

    return {"missing": missing, "trail": _step(state, "check_input")}


def have_everything(state: State) -> str:
    return "need_more_info" if state.get("missing") else "verify_payee"


def need_more_info(state: State) -> dict:
    # Ask, rather than guess. An agent that invents an account number is worse
    # than one that stops.
    return {
        "outcome": f"needs input: {', '.join(state['missing'])}",
        "trail": _step(state, "need_more_info"),
    }


# ---------------------------------------------------------------- payee

def verify_payee(state: State) -> dict:
    result = get_vop_adapter().verify(state["payee_name"], state["account"])
    return {"vop": result.as_dict(), "trail": _step(state, "verify_payee")}


def after_verify(state: State) -> str:
    # A wrong or uncheckable payee never reaches a human. Nobody should be
    # asked to approve a payment the check already failed.
    return "check_consent" if state["vop"]["status"] in ("MATCH", "PARTIAL") else "hold_or_reject"


# -------------------------------------------------------------- consent

def check_consent(state: State) -> dict:
    return {
        "consent": policy.check_consent(state.get("user_id", "user-001")),
        "trail": _step(state, "check_consent"),
    }


def after_consent(state: State) -> str:
    return "assess_risk" if state["consent"]["status"] == "valid" else "hold_or_reject"


# ----------------------------------------------------------------- risk

def assess_risk(state: State) -> dict:
    flags = policy.assess_risk(
        state["payee_name"], state["amount_minor"], state["vop"]["status"]
    )
    return {"risk_flags": flags, "trail": _step(state, "assess_risk")}


def after_risk(state: State) -> str:
    clean = state["vop"]["status"] == "MATCH" and not state["risk_flags"]
    return "execute_payment" if clean else "human_approval"


# ---------------------------------------------------------------- human

def human_approval(state: State) -> dict:
    answer = interrupt(
        {
            "question": "Approve this payment?",
            "payee": state["payee_name"],
            "amount": f"{state['amount_minor'] / 100:.2f} {state.get('currency', 'GBP')}",
            "payee_check": state["vop"],
            "why_you_are_being_asked": state["risk_flags"],
        }
    )
    return {"human_decision": answer, "trail": _step(state, "human_approval")}


def after_approval(state: State) -> str:
    return "execute_payment" if state.get("human_decision") == "approve" else "hold_or_reject"


# -------------------------------------------------------------- execute

def execute_payment(state: State) -> dict:
    # Defence in depth. The edges already prevent this, but money movement
    # gets a second lock that does not depend on the graph being wired right.
    approved = state.get("human_decision") == "approve"
    auto_ok = state["vop"]["status"] == "MATCH" and not state.get("risk_flags")
    if not (approved or auto_ok):
        raise RuntimeError("execute_payment reached without approval or a clean auto-approve")

    if PAYMENTS_MODE != "live":
        return {
            "execution": {"status": "simulated", "payment_id": None, "mode": "dry"},
            "outcome": "would execute (dry run)",
            "trail": _step(state, "execute_payment"),
        }

    from connectors import get_connector

    connector = get_connector("truelayer")
    status, payload, _ = connector.create_payment(
        amount_minor=state["amount_minor"],
        currency=state.get("currency", "GBP"),
        beneficiary={
            "type": "external_account",
            "account_holder_name": state["payee_name"],
            "account_identifier": {
                "type": "sort_code_account_number",
                "sort_code": state["account"]["sort_code"],
                "account_number": state["account"]["account_number"],
            },
            "reference": state.get("reference", "agent-payment"),
        },
        user={"id": str(uuid.uuid4()), "name": "Sandbox User", "email": "psu@example.com"},
        return_uri=state.get("return_uri") or RETURN_URI,
    )

    execution = {"http_status": status, "mode": "live"}
    if status in (200, 201):
        execution.update(
            {
                "payment_id": payload["id"],
                "status": payload.get("status"),
                # The PSU finishes Strong Customer Authentication here. The
                # agent cannot do this step, by design.
                "hpp_url": connector.hpp_url(
                    payload["id"], payload["resource_token"], state.get("return_uri") or RETURN_URI
                ),
            }
        )
    else:
        execution["error"] = payload

    return {
        "execution": execution,
        "outcome": f"payment created: {execution.get('status', 'error')}",
        "trail": _step(state, "execute_payment"),
    }


def reconcile(state: State) -> dict:
    """Ask the provider what actually happened, rather than trusting the
    response to the request that created it."""
    execution = dict(state.get("execution", {}))
    if execution.get("mode") == "live" and execution.get("payment_id"):
        from connectors import get_connector

        _, payload = get_connector("truelayer").get_payment(execution["payment_id"])
        execution["settled_status"] = payload.get("status")
    return {"execution": execution, "trail": _step(state, "reconcile")}


# ----------------------------------------------------------------- stop

def hold_or_reject(state: State) -> dict:
    if state.get("human_decision") and state["human_decision"] != "approve":
        reason = "human denied"
    elif state.get("consent", {}).get("status") not in (None, "valid"):
        reason = f"consent {state['consent']['status']}"
    else:
        reason = f"payee check {state['vop']['status']}"
    return {"outcome": f"held: {reason}", "trail": _step(state, "hold_or_reject")}


builder = StateGraph(State)
for name, fn in [
    ("check_input", check_input),
    ("need_more_info", need_more_info),
    ("verify_payee", verify_payee),
    ("check_consent", check_consent),
    ("assess_risk", assess_risk),
    ("human_approval", human_approval),
    ("execute_payment", execute_payment),
    ("reconcile", reconcile),
    ("hold_or_reject", hold_or_reject),
]:
    builder.add_node(name, fn)

builder.add_edge(START, "check_input")
builder.add_conditional_edges("check_input", have_everything,
                              {"need_more_info": "need_more_info", "verify_payee": "verify_payee"})
builder.add_conditional_edges("verify_payee", after_verify,
                              {"check_consent": "check_consent", "hold_or_reject": "hold_or_reject"})
builder.add_conditional_edges("check_consent", after_consent,
                              {"assess_risk": "assess_risk", "hold_or_reject": "hold_or_reject"})
builder.add_conditional_edges("assess_risk", after_risk,
                              {"execute_payment": "execute_payment", "human_approval": "human_approval"})
builder.add_conditional_edges("human_approval", after_approval,
                              {"execute_payment": "execute_payment", "hold_or_reject": "hold_or_reject"})
builder.add_edge("execute_payment", "reconcile")
builder.add_edge("need_more_info", END)
builder.add_edge("reconcile", END)
builder.add_edge("hold_or_reject", END)

graph = builder.compile()
