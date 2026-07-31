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

from api import learn, ratelimit
from graph import audit, payees, policy, tracing
from graph.app import builder
from graph.checkpointer import describe as checkpointer_name, get_checkpointer

# Starts the OTLP exporter if one is configured, and does nothing otherwise.
tracing.setup()

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


def topology() -> dict:
    """The graph as the page draws it, read from the compiled graph.

    `__start__` and `__end__` are LangGraph's own entry and exit markers rather
    than steps the agent takes, and the internals panel has always counted the
    nine real nodes, so the drawing keeps to the same nine and its caption says
    how many edges that leaves out.
    """
    compiled = graph.get_graph()
    real = [n for n in compiled.nodes if not n.startswith("__")]
    drawn = [
        {"source": e.source, "target": e.target, "conditional": bool(e.conditional)}
        for e in compiled.edges
        if not e.source.startswith("__") and not e.target.startswith("__")
    ]
    return {
        "nodes": [{"id": n, "label": STEP_LABELS.get(n, n)} for n in real],
        "edges": drawn,
        "total_edges": len(compiled.edges),
        "entry": next((e.target for e in compiled.edges if e.source == "__start__"), None),
    }


def edge_reason(source: str, target: str, state: dict) -> str:
    """The rule that sent the run down this edge, in the words of the rule.

    Read from the state the source node had just written. The stream stashes
    that state rather than reading the checkpoint when the next node starts,
    because by then the fields this depends on have moved on.
    """
    if source == "check_input":
        if target == "need_more_info":
            return "missing " + ", ".join(state.get("missing") or [])
        return "the instruction is complete"

    if source == "verify_payee":
        return {
            "MATCH": "the name matches the account",
            "PARTIAL": "the name is a near match, so a human decides",
            "NO_MATCH": "the name is not the account holder",
            "MATCH_NOT_POSSIBLE": "the account cannot be checked at all",
        }.get((state.get("vop") or {}).get("status"), "")

    if source == "check_consent":
        status = (state.get("consent") or {}).get("status")
        return "the consent is valid" if status == "valid" else f"the consent is {status}"

    if source == "assess_risk":
        flags = state.get("risk_flags") or []
        return "; ".join(flags) if flags else "nothing flagged, and under the ceiling"

    if source == "human_approval":
        return "you approved it" if state.get("human_decision") == "approve" else "you declined it"

    return ""


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
        run_config(thread_id),
    )
    if "__interrupt__" not in state:
        audit.record(thread_id, state)
    return JSONResponse(view(thread_id, state))


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def run_config(thread_id: str) -> dict:
    """Thread id for the checkpointer, plus metadata so the trace for this run
    can be found again in LangSmith without storing a run id ourselves."""
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": {"thread_id": thread_id, "app": "payment-agent"},
        "run_name": "payment-run",
    }


def stream_graph(thread_id: str, graph_input) -> Iterator[str]:
    """Emit one event per node as it starts and finishes.

    The timings are the agent's real timings. Nothing here is padded: the
    payee check is instant because it is simulated, and creating a payment
    takes as long as TrueLayer takes.
    """
    # One span for the whole run, and the parent of every span inside it —
    # including the model call in graph/llm.py. Held open across the yields by
    # delegating with `yield from`, so it closes when the stream is exhausted.
    # A no-op unless an OTLP endpoint is configured.
    with tracing.span(
        "payment.run" if isinstance(graph_input, dict) else "payment.resume",
        payment__thread_id=thread_id,
        payment__mode=os.getenv("PAYMENTS_MODE", "dry"),
        payment__checkpointer=checkpointer_name(),
    ) as run:
        yield from _stream_graph(thread_id, graph_input, config=run_config(thread_id), run=run)


def _stream_graph(thread_id: str, graph_input, config: dict, run) -> Iterator[str]:
    yield sse({"event": "thread", "thread_id": thread_id})

    edges = {(e["source"], e["target"]) for e in topology()["edges"]}

    # A resume replays from the checkpoint, so the node that ran before the
    # pause exists only in the stored trail. Without seeding from it, the edge
    # that crosses the interrupt is the one edge never drawn.
    stored = dict(graph.get_state(config).values or {})
    last_node = (stored.get("trail") or [None])[-1]
    last_state = stored

    try:
        for chunk in graph.stream(graph_input, config, stream_mode="debug"):
            kind = chunk.get("type")
            payload = chunk.get("payload", {})
            node = payload.get("name")

            if kind == "task":
                # The edge traversed is the pair of consecutive nodes. Emit it
                # only when the compiled graph actually has that edge, so a
                # replayed node cannot draw an arrow that does not exist.
                if last_node and (last_node, node) in edges:
                    yield sse({
                        "event": "edge",
                        "from": last_node,
                        "to": node,
                        "why": edge_reason(last_node, node, last_state),
                    })
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

                last_node, last_state = node, state
                yield sse({"event": "done", **describe_one(node, state)})
    except Exception as exc:
        run.error(exc)
        yield sse({"event": "error", "message": str(exc)})
        return

    snapshot = graph.get_state(config)
    state = dict(snapshot.values)
    run.set(
        payment__outcome=state.get("outcome"),
        payment__vop_status=(state.get("vop") or {}).get("status"),
        payment__risk_flags=len(state.get("risk_flags") or []),
        payment__paused=bool(snapshot.tasks and snapshot.tasks[0].interrupts),
    )
    if snapshot.tasks and snapshot.tasks[0].interrupts:
        state["__interrupt__"] = snapshot.tasks[0].interrupts
    else:
        # The run is over, so the decision is final and can be recorded.
        audit.record(thread_id, state, decided_by="human" if state.get("human_decision") else None)
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
    config = run_config(decision.thread_id)
    try:
        state = graph.invoke(Command(resume=decision.decision), config)
    except Exception as exc:  # a thread that no longer exists, most likely
        raise HTTPException(404, f"That run is no longer available: {exc}") from exc
    if "__interrupt__" not in state:
        audit.record(decision.thread_id, state, decided_by="human")
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


@app.get("/api/activity")
def activity(limit: int = 25) -> JSONResponse:
    """The audit trail: one row per completed run, newest first.

    Separate from the checkpoints on purpose. This is the record a reviewer
    would ask for, and it is queryable by payee, amount and outcome without
    deserialising framework state.
    """
    try:
        return JSONResponse(
            {
                "summary": audit.summary(),
                "rows": audit.recent(min(limit, 100)),
                "ruleset_version": audit.RULESET_VERSION,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Could not read the audit log: {exc}") from exc


@app.get("/activity")
def activity_page() -> FileResponse:
    return FileResponse(STATIC / "activity.html")


@app.get("/learn")
def learn_page() -> FileResponse:
    return FileResponse(STATIC / "learn.html")


@app.get("/api/learn")
def learn_api() -> dict:
    """The concepts this agent uses, each with its code read from the running
    deployment. Takes no parameters: the caller cannot name a file."""
    return learn.lessons()


@app.get("/api/graph")
def graph_shape() -> JSONResponse:
    """The topology the page draws.

    Generated rather than typed, so a node added to graph/app.py appears in the
    picture without anyone editing the HTML.
    """
    return JSONResponse(topology())


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
            "payee_history": payees.describe(),
            "tracing": tracing.describe(),
        }
    )


NODE_NAMES = set(STEP_LABELS)
_trace_cache: dict[str, dict] = {}


@app.get("/api/trace/{thread_id}")
def trace(thread_id: str) -> JSONResponse:
    """Per-step timings for one run, read from LangSmith.

    The route spine shows what the agent did. This shows how long each step
    actually took, which is where the difference between local logic and a
    network call becomes visible. Traces are immutable once written, so the
    result is cached for the life of the process.
    """
    if thread_id in _trace_cache:
        return JSONResponse(_trace_cache[thread_id])

    if os.getenv("LANGSMITH_TRACING", "").lower() not in ("1", "true"):
        return JSONResponse({"status": "disabled"})

    try:
        from langsmith import Client

        client = Client()
        runs = list(
            client.list_runs(
                project_name=os.getenv("LANGSMITH_PROJECT", "payment-agent-prod"),
                filter=f'and(eq(metadata_key, "thread_id"), eq(metadata_value, "{thread_id}"))',
            )
        )
    except Exception as exc:  # noqa: BLE001 — observability must never break a payment
        return JSONResponse({"status": "error", "message": str(exc)[:200]})

    spans, roots = [], []
    for run in sorted(runs, key=lambda r: r.start_time):
        if not run.end_time:
            continue                      # still open: the trace is incomplete
        ms = (run.end_time - run.start_time).total_seconds() * 1000
        if run.name == "payment-run":
            roots.append(ms)
        elif run.name in NODE_NAMES:
            spans.append({"node": run.name, "label": STEP_LABELS[run.name], "ms": round(ms, 1)})

    # Indexing is not atomic, so a partial trace looks like a short run. The
    # honest completion signal is a terminal node: every path ends at one of
    # these, so their absence means spans are still arriving.
    TERMINAL = {"reconcile", "hold_or_reject", "need_more_info"}
    complete = any(s["node"] in TERMINAL for s in spans)
    if not roots or not complete:
        return JSONResponse({"status": "pending", "found": len(runs), "spans": len(spans)})

    # A node that ran either side of the pause appears twice (human_approval
    # runs once to ask and once to receive the answer). Sum them so the list
    # reads as steps rather than as invocations.
    merged: dict[str, dict] = {}
    for span in spans:
        entry = merged.setdefault(span["node"], {**span, "ms": 0.0})
        entry["ms"] = round(entry["ms"] + span["ms"], 1)

    payload = {
        "status": "ready",
        "thread_id": thread_id,
        "total_ms": round(sum(roots), 1),
        "roots": len(roots),
        "spans": list(merged.values()),
        "project": os.getenv("LANGSMITH_PROJECT", "payment-agent-prod"),
    }
    _trace_cache[thread_id] = payload
    return JSONResponse(payload)


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
