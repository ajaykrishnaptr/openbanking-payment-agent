"""HTTP layer for the payment agent demo.

Three endpoints, matching the three moments in the flow:

  POST /api/run       submit an intent, run until the agent stops
  POST /api/decide    resume with the human's answer
  GET  /api/thread/x  read the current state (used after returning from the bank)

The graph does the deciding. This file only translates between HTTP and the
graph, and turns graph state into something the interface can render.

Persistence: the checkpointer is chosen by environment (see graph/checkpointer).
With POSTGRES_URL set it is Neon, so a run paused by one invocation can be
resumed by another. Without it, memory, which is right for local work.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from langgraph.types import Command

from api import ratelimit
from graph import policy
from graph.app import builder
from graph.checkpointer import describe as checkpointer_name, get_checkpointer

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(title="Open Banking payment agent", docs_url=None, redoc_url=None)
graph = builder.compile(checkpointer=get_checkpointer())

def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def return_uri_for(request: Request) -> str:
    """The callback the bank should send the payer back to.

    Derived from the request so localhost, a Vercel preview and production are
    each correct without a code change. Pin RETURN_URI when the deployment sits
    behind a domain the app cannot see, and remember every value used here has
    to be registered in the TrueLayer console first.
    """
    pinned = os.getenv("RETURN_URI")
    if pinned:
        return pinned

    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        proto = request.headers.get("x-forwarded-proto", "https")
        return f"{proto}://{forwarded_host}/callback"

    return str(request.base_url).rstrip("/") + "/callback"


class Intent(BaseModel):
    payee_name: str = Field(min_length=1, max_length=140)
    sort_code: str = Field(min_length=6, max_length=8)
    account_number: str = Field(min_length=6, max_length=10)
    amount: float = Field(gt=0, le=1_000_000)
    reference: str = Field(default="agent-payment", max_length=60)


class Decision(BaseModel):
    thread_id: str
    decision: str  # "approve" | "deny"


# --------------------------------------------------------------- view model

STEP_LABELS = {
    "check_input": "Read the instruction",
    "verify_payee": "Check the payee against the account",
    "check_consent": "Check the consent is still valid",
    "assess_risk": "Score the risk",
    "human_approval": "Ask a human",
    "execute_payment": "Create the payment",
    "reconcile": "Confirm with the bank",
    "hold_or_reject": "Stop",
    "need_more_info": "Ask for what is missing",
}


def describe_one(node: str, state: dict) -> dict:
    """One finished node, with the detail that makes it meaningful rather
    than a tick. Shared by the batch response and the stream."""
    detail = ""
    tone = "done"

    if node == "verify_payee":
        vop = state.get("vop", {})
        detail = f"{vop.get('status')} · confidence {vop.get('confidence')}"
        if vop.get("matched_name"):
            detail += f" · bank has “{vop['matched_name']}”"
        if vop.get("status") in ("NO_MATCH", "MATCH_NOT_POSSIBLE"):
            tone = "refused"
        elif vop.get("status") == "PARTIAL":
            tone = "attention"

    elif node == "check_consent":
        consent = state.get("consent", {})
        detail = consent.get("status", "")
        if consent.get("consent_id"):
            detail += f" · {consent['consent_id']}"
        if consent.get("status") != "valid":
            tone = "refused"

    elif node == "assess_risk":
        flags = state.get("risk_flags", [])
        detail = "no flags" if not flags else f"{len(flags)} flag{'s' if len(flags) > 1 else ''}"
        tone = "done" if not flags else "attention"

    elif node == "human_approval":
        decision = state.get("human_decision")
        detail = {"approve": "approved", "deny": "declined"}.get(decision, "waiting")
        tone = "attention" if decision is None else ("done" if decision == "approve" else "refused")

    elif node == "execute_payment":
        execution = state.get("execution", {})
        http_status = execution.get("http_status")
        if http_status and http_status >= 400:
            # The agent decided to pay and the provider said no. That is not
            # a refusal by the agent, and must not be shown as one.
            problem = execution.get("error", {})
            fields = problem.get("errors") or {}
            first = next(iter(fields.values()), [problem.get("detail", "rejected")])
            detail = f"provider rejected it: {first[0]}"
            tone = "refused"
        else:
            detail = execution.get("status") or execution.get("mode", "")

    elif node == "reconcile":
        detail = state.get("execution", {}).get("settled_status", "")

    elif node in ("hold_or_reject", "need_more_info"):
        detail = state.get("outcome", "")
        tone = "refused"

    return {"node": node, "label": STEP_LABELS.get(node, node), "detail": detail, "tone": tone}


def describe(state: dict) -> list[dict]:
    """Every step so far, for a non-streaming response."""
    steps = []
    for node in state.get("trail", []):
        detail = ""
        tone = "done"

        if node == "verify_payee":
            vop = state.get("vop", {})
            detail = f"{vop.get('status')} · confidence {vop.get('confidence')}"
            if vop.get("matched_name"):
                detail += f" · bank has “{vop['matched_name']}”"
            if vop.get("status") in ("NO_MATCH", "MATCH_NOT_POSSIBLE"):
                tone = "refused"
            elif vop.get("status") == "PARTIAL":
                tone = "attention"

        elif node == "check_consent":
            consent = state.get("consent", {})
            detail = consent.get("status", "")
            if consent.get("consent_id"):
                detail += f" · {consent['consent_id']}"
            if consent.get("status") != "valid":
                tone = "refused"

        elif node == "assess_risk":
            flags = state.get("risk_flags", [])
            detail = "no flags" if not flags else f"{len(flags)} flag{'s' if len(flags) > 1 else ''}"
            tone = "done" if not flags else "attention"

        elif node == "human_approval":
            decision = state.get("human_decision")
            detail = {"approve": "approved", "deny": "declined"}.get(decision, "waiting")
            tone = "attention" if decision is None else ("done" if decision == "approve" else "refused")

        elif node == "execute_payment":
            execution = state.get("execution", {})
            detail = execution.get("status") or execution.get("mode", "")

        elif node == "reconcile":
            detail = state.get("execution", {}).get("settled_status", "")

        elif node in ("hold_or_reject", "need_more_info"):
            detail = state.get("outcome", "")
            tone = "refused"

        steps.append({"node": node, "label": STEP_LABELS.get(node, node), "detail": detail, "tone": tone})
    return steps


_cases_cache: list | None = None


def covering_case(state: dict) -> dict | None:
    """Which eval case asserts the path this run just walked.

    Matched on the exact node sequence, with the outcome as a tiebreaker
    because two cases can share a path for different reasons (an expired
    consent and a missing one both stop at the same station).
    """
    global _cases_cache
    if _cases_cache is None:
        path = ROOT / "data" / "eval-results.json"
        _cases_cache = json.loads(path.read_text()).get("cases", []) if path.exists() else []

    trail = state.get("trail") or []
    if not trail:
        return None

    matches = [c for c in _cases_cache if c.get("trail") == trail]
    if not matches:
        return None

    if len(matches) > 1:
        outcome = state.get("outcome")
        narrowed = [c for c in matches if c.get("outcome") == outcome]
        matches = narrowed or matches

    if len(matches) > 1:
        # Still ambiguous, because two cases can walk the same path for the
        # same stated reason. Fall back to the inputs: a run carrying an
        # injection string in the reference belongs to the adversarial case,
        # a plain one does not.
        def closeness(case: dict) -> int:
            case_input = case.get("input", {})
            score = 0
            for field in ("payee_name", "amount_minor"):
                if case_input.get(field) == state.get(field):
                    score += 1
            case_reference = case_input.get("reference") or ""
            live_reference = state.get("reference") or ""
            if case_reference == live_reference:
                score += 2
            elif case_reference:
                # The case is about a specific reference string and this run
                # carries a different one, so it is the weaker match.
                score -= 1
            return score

        matches = sorted(matches, key=closeness, reverse=True)

    case = matches[0]
    return {
        "id": case["id"],
        "split": case["split"],
        "protects": case["protects"],
        "passed": case["passed"],
    }


def auto_approved(state: dict) -> dict | None:
    """Why no human was asked, when none was asked.

    A blank station reads as though the agent slipped past the approval. It
    did not: the policy permits this payment to proceed alone, and the reasons
    are worth stating on the page.
    """
    trail = state.get("trail") or []
    if "human_approval" in trail or "execute_payment" not in trail:
        return None

    ceiling = policy.AUTO_APPROVE_CEILING_MINOR // 100
    amount = (state.get("amount_minor") or 0) / 100
    return {
        "reasons": [
            "the payee name matched the account exactly",
            f"the amount is {amount:,.2f}, under the {ceiling:,} ceiling",
            "this payee has been paid before",
        ],
        "summary": f"No approval needed under the rules you can read in policy.py",
    }


def view(thread_id: str, state: dict) -> dict:
    interrupts = state.get("__interrupt__")
    payload = interrupts[0].value if interrupts else None
    return {
        "thread_id": thread_id,
        "status": "waiting_approval" if payload else "finished",
        "steps": describe(state),
        "approval": payload,
        "outcome": state.get("outcome"),
        "execution": state.get("execution"),
        "vop_provider": (state.get("vop") or {}).get("provider"),
        "vop_status": (state.get("vop") or {}).get("status"),
        "covered_by": covering_case(state) if not state.get("__interrupt__") else None,
        "auto_approved": auto_approved(state),
    }


# ----------------------------------------------------------------- endpoints

@app.post("/api/run")
def run(intent: Intent, request: Request) -> JSONResponse:
    allowed, message = ratelimit.check(client_ip(request))
    if not allowed:
        raise HTTPException(429, message)

    thread_id = f"web-{uuid.uuid4()}"
    state = graph.invoke(
        {
            "return_uri": return_uri_for(request),
            "user_id": "user-001",
            "payee_name": intent.payee_name.strip(),
            "account": {
                "sort_code": intent.sort_code.replace("-", "").strip(),
                "account_number": intent.account_number.strip(),
            },
            "amount_minor": int(round(intent.amount * 100)),
            "currency": "GBP",
            "reference": intent.reference.strip() or "agent-payment",
        },
        {"configurable": {"thread_id": thread_id}},
    )
    return JSONResponse(view(thread_id, state))


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def stream_graph(thread_id: str, graph_input) -> Iterator[str]:
    """Emit one event per node as it starts and finishes.

    The timings are the agent's real timings. Nothing here is padded: the
    payee check is instant because it is simulated, and creating a payment
    takes as long as TrueLayer takes.
    """
    config = {"configurable": {"thread_id": thread_id}}
    yield sse({"event": "thread", "thread_id": thread_id})

    try:
        for chunk in graph.stream(graph_input, config, stream_mode="debug"):
            kind = chunk.get("type")
            payload = chunk.get("payload", {})
            node = payload.get("name")

            if kind == "task":
                yield sse({"event": "start", "node": node, "label": STEP_LABELS.get(node, node)})

            elif kind == "task_result":
                if payload.get("interrupts"):
                    # It asked a human. The approval card takes over from here.
                    yield sse({"event": "waiting", "node": node})
                    continue

                # The checkpoint is written AFTER this event, so reading state
                # here returns the values from before this node ran. Merge the
                # node's own writes on top, or every step reports stale data.
                state = dict(graph.get_state(config).values)
                writes = payload.get("result") or {}
                state.update(writes if isinstance(writes, dict) else dict(writes))

                yield sse({"event": "done", **describe_one(node, state)})
    except Exception as exc:
        yield sse({"event": "error", "message": str(exc)})
        return

    snapshot = graph.get_state(config)
    state = dict(snapshot.values)
    if snapshot.tasks and snapshot.tasks[0].interrupts:
        state["__interrupt__"] = snapshot.tasks[0].interrupts
    yield sse({"event": "final", "view": view(thread_id, state)})


@app.post("/api/run/stream")
def run_stream(intent: Intent, request: Request) -> StreamingResponse:
    allowed, message = ratelimit.check(client_ip(request))
    if not allowed:
        raise HTTPException(429, message)

    thread_id = f"web-{uuid.uuid4()}"
    graph_input = {
        "return_uri": return_uri_for(request),
        "user_id": "user-001",
        "payee_name": intent.payee_name.strip(),
        "account": {
            "sort_code": intent.sort_code.replace("-", "").strip(),
            "account_number": intent.account_number.strip(),
        },
        "amount_minor": int(round(intent.amount * 100)),
        "currency": "GBP",
        "reference": intent.reference.strip() or "agent-payment",
    }
    return StreamingResponse(
        stream_graph(thread_id, graph_input),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/decide/stream")
def decide_stream(decision: Decision) -> StreamingResponse:
    if decision.decision not in ("approve", "deny"):
        raise HTTPException(400, "decision must be approve or deny")
    return StreamingResponse(
        stream_graph(decision.thread_id, Command(resume=decision.decision)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/decide")
def decide(decision: Decision, request: Request) -> JSONResponse:
    if decision.decision not in ("approve", "deny"):
        raise HTTPException(400, "decision must be approve or deny")
    config = {"configurable": {"thread_id": decision.thread_id}}
    try:
        state = graph.invoke(Command(resume=decision.decision), config)
    except Exception as exc:  # a thread that no longer exists, most likely
        raise HTTPException(404, f"That run is no longer available: {exc}") from exc
    return JSONResponse(view(decision.thread_id, state))


@app.get("/api/thread/{thread_id}")
def read_thread(thread_id: str) -> JSONResponse:
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    if not snapshot.values:
        raise HTTPException(404, "unknown run")
    state = dict(snapshot.values)
    if snapshot.tasks and snapshot.tasks[0].interrupts:
        state["__interrupt__"] = snapshot.tasks[0].interrupts
    return JSONResponse(view(thread_id, state))


@app.get("/api/payment/{payment_id}")
def payment_status(payment_id: str) -> JSONResponse:
    """Where the payment stands, straight from the provider.

    Used by the callback page after the bank sends the payer back. The status
    reported here is TrueLayer's, not something this app remembers, because
    the whole point of reconciling is to stop trusting our own record.
    """
    from connectors import get_connector

    status_code, payload = get_connector("truelayer").get_payment(payment_id)
    if status_code >= 400:
        raise HTTPException(status_code, "That payment could not be read from the provider.")
    return JSONResponse(
        {
            "payment_id": payment_id,
            "status": payload.get("status"),
            "created_at": payload.get("created_at"),
            "amount_in_minor": payload.get("amount_in_minor"),
            "currency": payload.get("currency"),
        }
    )


_stats_cache: dict = {"at": 0.0, "value": None}


def checkpoint_stats() -> dict | None:
    """How many paused runs the database is actually holding.

    Cached for a minute, because this runs on a page load and the number moves
    slowly. Returns None when running on the in-memory checkpointer.
    """
    url = os.getenv("POSTGRES_URL_NON_POOLING") or os.getenv("POSTGRES_URL")
    if not url or checkpointer_name() != "postgres":
        return None

    if time.time() - _stats_cache["at"] < 60 and _stats_cache["value"]:
        return _stats_cache["value"]

    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=8) as conn, conn.cursor() as cur:
            cur.execute("select count(*), count(distinct thread_id) from checkpoints")
            rows, threads = cur.fetchone()
        value = {"checkpoints": rows, "threads": threads}
    except Exception:
        return None

    _stats_cache.update({"at": time.time(), "value": value})
    return value


@app.get("/api/internals")
def internals() -> JSONResponse:
    """What the page needs to show its own workings.

    The graph shape is read from the compiled graph, so the diagram cannot
    drift from the code. The eval numbers come from the last recorded
    experiment rather than from a claim typed into the HTML.
    """
    compiled = graph.get_graph()
    nodes = [n for n in compiled.nodes if not n.startswith("__")]
    conditional = sum(1 for edge in compiled.edges if edge.conditional)

    stored = checkpoint_stats()

    results_file = ROOT / "data" / "eval-results.json"
    evals = json.loads(results_file.read_text()) if results_file.exists() else None

    golden_file = ROOT / "data" / "golden.json"
    golden = json.loads(golden_file.read_text()) if golden_file.exists() else {}

    return JSONResponse(
        {
            "graph": {
                "nodes": len(nodes),
                "node_names": nodes,
                "edges": len(compiled.edges),
                "conditional_edges": conditional,
                "mermaid": compiled.draw_mermaid(),
            },
            "evals": evals,
            "provenance": golden.get("provenance"),
            "checkpointer": checkpointer_name(),
            "stored": stored,
            "vop_provider": os.getenv("VOP_PROVIDER", "stub"),
        }
    )


@app.get("/callback")
def callback() -> FileResponse:
    """Where the bank sends the payer after Strong Customer Authentication."""
    return FileResponse(STATIC / "callback.html")


@app.get("/api/config")
def config() -> dict:
    """What the interface needs to tell the truth about this deployment."""
    return {
        "payments_mode": os.getenv("PAYMENTS_MODE", "dry"),
        "checkpointer": checkpointer_name(),
        "vop_provider": os.getenv("VOP_PROVIDER", "stub"),
        "directory": [
            {"name": "Pinguin Pfannkuchen GmbH", "sort_code": "040668", "account_number": "00000871"},
            {"name": "Waffelwerk Bremen GmbH", "sort_code": "040668", "account_number": "00000872"},
        ],
        "auto_approve_ceiling": 1000,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
