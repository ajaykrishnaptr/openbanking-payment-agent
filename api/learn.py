"""The LangGraph concepts this agent actually uses, each with the code that
does it.

The excerpts are not stored here. Each lesson names a file and a symbol, and the
source is read out of the running deployment and located by parsing it, so a
snippet cannot drift from the code it claims to explain. Rename a function and
the lesson fails loudly instead of describing something that no longer exists.

Only concepts this repository demonstrates are listed. Cycles, Send, subgraphs
and time travel are absent on purpose: a page that teaches what the code cannot
show is the same failure as a hardcoded number.

Nothing here takes a path from the caller. The endpoint has no parameters at
all, and `_extract` re-checks the file against ALLOWED anyway, because a reader
that can be pointed at an arbitrary path is one refactor away from serving
secrets/.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every file an excerpt may come from. All of these ship with the deployment —
# see includeFiles in vercel.json. A file outside this set is refused even if a
# lesson names it.
ALLOWED = {
    "graph/app.py",
    "graph/policy.py",
    "graph/checkpointer.py",
    "graph/llm.py",
    "graph/vop.py",
    "api/server.py",
}

LESSONS = [
    {
        "id": "state",
        "concept": "State, and the reducer that merges it",
        "what": "A LangGraph graph is a state machine. The state is a TypedDict, and every "
                "key can declare how two writes to it combine.",
        "why": "The trail is annotated with operator.add, so each node returns only the step "
               "it just took and LangGraph concatenates. Without a reducer the last write "
               "wins, and two nodes writing at once raise rather than pick a winner.",
        "file": "graph/app.py",
        "symbol": "State",
    },
    {
        "id": "partial-updates",
        "concept": "A node returns only what it changed",
        "what": "A node is a plain function. It takes the state and returns a dict of just "
                "the keys it touched; everything it omits is left alone.",
        "why": "check_input reports what is missing and nothing else. It never has to know "
               "about consent, risk or payments, which is what keeps each node testable on "
               "its own.",
        "file": "graph/app.py",
        "symbol": "check_input",
    },
    {
        "id": "conditional-edges",
        "concept": "Conditional edges are where the product rules live",
        "what": "add_conditional_edges takes a function that returns the name of the next "
                "node. That function is the branch.",
        "why": "This is the rule that decides whether money moves without a person. It is "
               "three lines of if/else over state, so it can be replayed and tested "
               "without a model in the loop.",
        "file": "graph/app.py",
        "symbol": "after_risk",
    },
    {
        "id": "fail-closed",
        "concept": "A failed check ends the run where it stands",
        "what": "A branch can route straight to a terminal node instead of carrying on.",
        "why": "A payee that does not match never reaches consent, and never reaches a "
               "human. Nobody should be asked to approve a payment the check already "
               "failed, because that is how rubber-stamping starts.",
        "file": "graph/app.py",
        "symbol": "after_verify",
    },
    {
        "id": "model-placement",
        "concept": "Where the model is allowed to run",
        "what": "There is an LLM in this agent, and it sits in exactly one place: deciding "
                "whether two spellings of a company name are the same company.",
        "why": "The deterministic checks run first and settle what they can: an unknown "
               "account is uncheckable, an exact string match is a MATCH. The model is asked "
               "only about the ambiguous middle, where a string ratio is weakest and language "
               "is the actual problem. If it is unavailable, over budget or malformed, the "
               "run falls back to the ratio rather than failing.",
        "file": "graph/vop.py",
        "symbol": "SemanticVoP",
    },
    {
        "id": "rules-decide",
        "concept": "The model never decides",
        "what": "The model returns a verdict about a name. It does not score risk, and it "
                "does not choose a branch.",
        "why": "Risk is scored here, in rules over the amount, the payee history and the "
               "verdict the check produced. Nothing in this function calls a model, which is "
               "what lets an auditor be shown why one payment auto-approved and an identical "
               "one did not.",
        "file": "graph/policy.py",
        "symbol": "assess_risk",
    },
    {
        "id": "interrupt",
        "concept": "Stopping mid-graph for a human",
        "what": "interrupt() halts execution and hands a payload out to whoever is waiting. "
                "The run is written to the checkpointer as it stands.",
        "why": "The node that asks a person is a node like any other. On resume it re-runs "
               "from the top and interrupt() returns the answer instead of raising, which is "
               "why the earlier checks are not repeated.",
        "file": "graph/app.py",
        "symbol": "human_approval",
    },
    {
        "id": "resume",
        "concept": "Resuming with Command(resume=…)",
        "what": "A later request continues a paused thread by invoking the graph with a "
                "Command carrying the decision.",
        "why": "The approval arrives in a different HTTP request, and on serverless very "
               "likely a different process. The thread_id is the only thing tying them "
               "together.",
        "file": "api/server.py",
        "symbol": "decide",
    },
    {
        "id": "checkpointer",
        "concept": "Where a paused run lives",
        "what": "The checkpointer is what makes a pause outlive the request that created it. "
                "Memory locally, a database in production.",
        "why": "An interrupt is only useful if the state survives. Chosen by environment, so "
               "the eval suite needs no database and the deployment needs no code change.",
        "file": "graph/checkpointer.py",
        "symbol": "get_checkpointer",
    },
]


def _extract(rel: str, symbol: str) -> dict | None:
    """Return the source of one top-level symbol, located by parsing the file.

    Line numbers are read rather than recorded, so they stay correct as the file
    moves around. Returns None when the file or symbol is gone, which the page
    surfaces instead of hiding.
    """
    if rel not in ALLOWED:
        return None

    path = ROOT / rel
    if not path.is_file():
        return None

    source = path.read_text()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name != symbol:
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        lines = source.splitlines()[start - 1 : node.end_lineno]
        return {"code": "\n".join(lines), "line": start, "lines": len(lines)}

    return None


def lessons() -> dict:
    """Every lesson, with its excerpt read from the deployed source."""
    out = []
    for lesson in LESSONS:
        excerpt = _extract(lesson["file"], lesson["symbol"])
        out.append(
            {
                "id": lesson["id"],
                "concept": lesson["concept"],
                "what": lesson["what"],
                "why": lesson["why"],
                "file": lesson["file"],
                "symbol": lesson["symbol"],
                "code": excerpt["code"] if excerpt else None,
                "line": excerpt["line"] if excerpt else None,
                "missing": excerpt is None,
            }
        )
    return {"lessons": out, "missing": sum(1 for l in out if l["missing"])}
