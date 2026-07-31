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

import operator
import os
import uuid
from typing import Annotated, TypedDict

from langgraph.config import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from . import payees, policy
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
# Fixed namespace, so the idempotency key for a run depends on nothing but its
# thread id. Changing this value would make every in-flight retry look new.
PAYMENT_NAMESPACE = uuid.UUID("6f1a9d64-1b3f-4a1e-9b0e-7c9a5f2d8e31")


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
    # Append-only, merged by a reducer. A node returns only the step it just
    # took and LangGraph concatenates; nothing reads the trail to rewrite it.
    # This is also what makes the key safe if two nodes ever run in parallel,
    # where a plain key raises rather than pick a winner.
    trail: Annotated[list[str], operator.add]


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

    return {"missing": missing, "trail": ["check_input"]}


def have_everything(state: State) -> str:
    return "need_more_info" if state.get("missing") else "verify_payee"


def need_more_info(state: State) -> dict:
    # Ask, rather than guess. An agent that invents an account number is worse
    # than one that stops.
    return {
        "outcome": f"needs input: {', '.join(state['missing'])}",
        "trail": ["need_more_info"],
    }


# ---------------------------------------------------------------- payee

def verify_payee(state: State) -> dict:
    result = get_vop_adapter().verify(state["payee_name"], state["account"])
    return {"vop": result.as_dict(), "trail": ["verify_payee"]}


def after_verify(state: State) -> str:
    # A wrong or uncheckable payee never reaches a human. Nobody should be
    # asked to approve a payment the check already failed.
    return "check_consent" if state["vop"]["status"] in ("MATCH", "PARTIAL") else "hold_or_reject"


# -------------------------------------------------------------- consent

def check_consent(state: State) -> dict:
    return {
        "consent": policy.check_consent(state.get("user_id", "user-001")),
        "trail": ["check_consent"],
    }


def after_consent(state: State) -> str:
    return "assess_risk" if state["consent"]["status"] == "valid" else "hold_or_reject"


# ----------------------------------------------------------------- risk

def assess_risk(state: State) -> dict:
    flags = policy.assess_risk(
        state["payee_name"], state["amount_minor"], state["vop"]["status"]
    )
    return {"risk_flags": flags, "trail": ["assess_risk"]}


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
    return {"human_decision": answer, "trail": ["human_approval"]}


def after_approval(state: State) -> str:
    return "execute_payment" if state.get("human_decision") == "approve" else "hold_or_reject"


# -------------------------------------------------------------- execute

def execute_payment(state: State, config: RunnableConfig) -> dict:
    # Defence in depth. The edges already prevent this, but money movement
    # gets a second lock that does not depend on the graph being wired right.
    approved = state.get("human_decision") == "approve"
    auto_ok = state["vop"]["status"] == "MATCH" and not state.get("risk_flags")
    if not (approved or auto_ok):
        raise RuntimeError("execute_payment reached without approval or a clean auto-approve")

    # The checkpoint for this node is written after it returns, so a crash
    # between the POST and that write leaves a payment at the bank that the
    # graph has no record of, and the resume runs this node again. The second
    # attempt must present the SAME idempotency key or the provider reads it as
    # a second payment. The thread id is the one value that survives a replay
    # unchanged, so the key is derived from it rather than generated.
    thread_id = (config.get("configurable") or {}).get("thread_id")
    idempotency_key = str(uuid.uuid5(PAYMENT_NAMESPACE, thread_id)) if thread_id else None

    if PAYMENTS_MODE != "live":
        return {
            "execution": {"status": "simulated", "payment_id": None, "mode": "dry"},
            "outcome": "would execute (dry run)",
            "trail": ["execute_payment"],
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
        idempotency_key=idempotency_key,
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
        "trail": ["execute_payment"],
    }


def reconcile(state: State) -> dict:
    """Ask the provider what actually happened, rather than trusting the
    response to the request that created it."""
    execution = dict(state.get("execution", {}))
    if execution.get("mode") == "live" and execution.get("payment_id"):
        from connectors import get_connector

        _, payload = get_connector("truelayer").get_payment(execution["payment_id"])
        execution["settled_status"] = payload.get("status")

        # This payee is no longer new, so the next payment to them will not
        # raise the first-payment flag. Deliberately gated on a payment that
        # really exists: a dry run decided to pay but did not, and the count
        # here is a factual claim. Writing it never fails the run.
        payees.record_payment(state["payee_name"])
    return {"execution": execution, "trail": ["reconcile"]}


# ----------------------------------------------------------------- stop

def hold_or_reject(state: State) -> dict:
    if state.get("human_decision") and state["human_decision"] != "approve":
        reason = "human denied"
        # A person refusing this payee is worth remembering: from here on, every
        # payment to them asks a person too. Only ever more cautious, and
        # reversible with payees.clear_flag when the refusal was about the
        # amount rather than the payee.
        amount = state.get("amount_minor")
        payees.flag(
            state["payee_name"],
            f"a human denied a payment of {amount / 100:.2f} {state.get('currency', 'GBP')}"
            if amount else "a human denied a payment",
        )
    elif state.get("consent", {}).get("status") not in (None, "valid"):
        reason = f"consent {state['consent']['status']}"
    else:
        reason = f"payee check {state['vop']['status']}"
    return {"outcome": f"held: {reason}", "trail": ["hold_or_reject"]}


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
