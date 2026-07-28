# Open Banking payment agent

**Live demo: https://openbanking-payment-agent.vercel.app**

A program that sends bank payments for you. Before it sends anything it checks that the name you
typed really owns the account, and if something looks wrong it stops and asks a human. When it
does pay, the bank still asks that person to authenticate, exactly as it would for a payment made
by hand.

Everything runs on the TrueLayer sandbox with test money. Built with Claude Code: the product,
architecture and routing decisions are mine, the implementation was written under my direction.

---

## Why this exists

Most agent demos answer "can it act". The harder question for anything touching money is "where
does it stop, and who decided". This was built to answer that concretely, in a domain with real
constraints.

### Learning objectives

The build was structured around six things worth understanding properly:

1. **LangGraph as a state machine rather than a chatbot wrapper.** State, nodes, fixed edges,
   conditional edges, and the difference between a router that returns a node name and a node
   that returns a state update.
2. **Human-in-the-loop as a first-class primitive.** How `interrupt()` suspends a run, what a
   checkpointer stores, and how `Command(resume=...)` continues without replaying finished work.
3. **Durability across a stateless runtime.** Why a paused run dies on serverless without an
   external checkpointer, and what changes when the process cannot be trusted to survive.
4. **Evaluation as acceptance criteria.** Writing cases that assert the path a run took, not only
   where it ended, and versioning them so a regression is visible.
5. **Designing around a capability that does not exist yet.** Verification of Payee is unavailable
   to developers, so the product had to accommodate its absence honestly.
6. **Where a language model belongs in a money flow.** Currently nowhere near the decision, for
   reasons set out in [Rules at the decision](#rules-at-the-decision-language-at-the-edges).

---

## What is real, and what is not

| | |
|---|---|
| **Real** | OAuth client credentials, request signing (`Tl-Signature`), `POST /v3/payments`, payment status read back from the provider, the bank's own authentication page, the pause surviving in Postgres |
| **Simulated** | Verification of Payee. TrueLayer's VoP service is expected H2 2026 and is not available on a developer account. Verified directly: the `verification` scope resolves to audience `data_api`, and every payee-verification path on the Payments API returns 404 |
| **Also simulated** | Consent records and payee history, which are Python dicts in `graph/policy.py` |
| **Never real** | The money. TrueLayer sandbox only, and the interface says so where it matters |

The simulation is labelled in the interface at the point of use. A demo that quietly fakes a bank
check would have been faster to build and worth nothing.

---

## Architecture

```
Browser
   │  POST /api/run/stream            server-sent events, one per node
   ▼
FastAPI ── LangGraph app
   │
   │   check_input ─┬─ need_more_info                    (missing or invalid input)
   │                └─ verify_payee ─┬─ hold_or_reject   (NO_MATCH, MATCH_NOT_POSSIBLE)
   │                                 └─ check_consent ─┬─ hold_or_reject  (expired, missing)
   │                                                   └─ assess_risk
   │                                                        ├─ execute_payment  (clean, under ceiling)
   │                                                        └─ human_approval
   │                                                             ├─ execute_payment (approved)
   │                                                             └─ hold_or_reject  (declined)
   │   execute_payment → reconcile → END
   │
   │        interrupt() at human_approval
   ▼
Neon Postgres          checkpoints, rate limits
   │
   ▼  POST /api/decide/stream → Command(resume="approve")
TrueLayer /v3/payments → hosted page → bank authentication → /callback
```

Nine nodes, fifteen edges, ten of them conditional. The demo page reports those numbers by
reading the compiled graph, so they cannot drift from the code.

### State

```python
class State(TypedDict, total=False):
    user_id, payee_name, account, amount_minor, currency, reference, return_uri  # input
    missing, vop, consent, risk_flags, human_decision, execution, outcome        # working
    trail: list[str]                                                             # the path taken
```

`trail` accumulates each node's name as it completes. It drives the interface and the eval suite
asserts against it.

### Two locks on money movement

Conditional edges prevent `execute_payment` being reached without approval. Inside the node, a
second check raises if it runs anyway, so a miswired edge cannot pay anyone.

```python
approved = state.get("human_decision") == "approve"
auto_ok = state["vop"]["status"] == "MATCH" and not state.get("risk_flags")
if not (approved or auto_ok):
    raise RuntimeError("execute_payment reached without approval or a clean auto-approve")
```

### Rules at the decision, language at the edges

No model runs inside the decision path, and that is deliberate. Whether a payment may proceed is
a policy question with an audit trail, so it lives in readable rules in `graph/policy.py`: a
payee status of `NO_MATCH` stops the run before consent is checked, an amount above the ceiling
always asks a human, a previously flagged payee escalates.

A model belongs at the edges, parsing free text into a structured instruction and explaining
outcomes. That is the next addition, and it changes the threat model: today a hidden instruction
in the payment reference is inert because nothing reads it.

---

## Technical details worth knowing

### The pause

`human_approval` calls `interrupt()` with a payload describing what is being asked and why. The
run stops, LangGraph writes state to the checkpointer, and the HTTP request returns. A later
request calls `Command(resume="approve")` and the graph continues inside the node that asked.
Completed nodes do not run again, which the `resume-no-replay` case asserts.

### Streaming the run

The interface shows each step as it happens using `graph.stream(..., stream_mode="debug")`, which
emits a `task` event when a node starts and `task_result` when it finishes. The server forwards
these as server-sent events. Timings are the agent's real timings: local checks resolve almost
instantly, and the spinner is visible on "Create the payment" because that call waits on
TrueLayer.

One trap: at `task_result` time the checkpoint has not been written, so reading state there
returns pre-node values. The node's own writes must be merged on top or every step reports stale
data.

### Persistence

`graph/checkpointer.py` selects by environment. With `POSTGRES_URL` present it is Neon Postgres,
without it memory, so local development and the eval suite need no configuration.

Three Neon specifics, each of which caused a real failure before being handled:

- **Use the pooled connection string.** Serverless opens many short-lived connections and a direct
  endpoint runs out of slots.
- **PgBouncer runs in transaction mode**, so psycopg's server-side prepared statements cannot
  survive between transactions. The pool opens with `prepare_threshold=None`.
- **Neon suspends an idle database** and drops its connections. A pool that keeps handing those
  out fails with `consuming input failed: server closed the connection unexpectedly`. Handled
  with `check=ConnectionPool.check_connection`, `max_idle=60` and `max_lifetime=600`.

DDL runs through the **unpooled** string in `scripts/setup_db.py`, because schema changes through
a transaction-mode pooler are unreliable.

### Verification of Payee, behind an adapter

`graph/vop.py` defines a `VoPAdapter` protocol returning `MATCH`, `PARTIAL`, `NO_MATCH` or
`MATCH_NOT_POSSIBLE`. `StubVoP` normalises legal forms before comparing, so "Pinguin Pfannkuchen
GmbH" against "Pinguin Pfannkuchen Ltd" is a PARTIAL and not a rejection. `TrueLayerVoP` raises
`NotImplementedError` on purpose, so nobody ships believing the real service ran. When TrueLayer
releases VoP, one implementation is added and `get_vop_adapter()` changes.

### The callback

TrueLayer returns the payer to `return_uri`, worked out per request: `RETURN_URI` if set,
otherwise `x-forwarded-host` and `x-forwarded-proto` (what Vercel sends), otherwise the request
origin. **Every value must be registered in the TrueLayer console first**, or the payment fails
with HTTP 400 and `"Return URI must be added in the TrueLayer Console before use"`. Matching is
exact with no wildcards, so preview deployments need `RETURN_URI` pinned to the production URL.

The callback page reads the outcome from the provider by payment id rather than from local state,
so it needs no session and works on serverless. It handles a completed payment, a failure, and
`tl_hpp_abandoned`, which TrueLayer sends when the payer closes the bank screen without
authenticating. That case is the escape hatch working, and it is worded that way.

### Rate limiting

Two layers, both in Postgres: 20 runs per hour per address, 400 per day globally. Counting is a
single `insert ... on conflict do update ... returning`, so concurrent requests cannot both read
the same count and both decide they are within the limit. A module-level dict cannot do this on
serverless, where each instance keeps its own counters and the effective limit becomes the stated
one multiplied by the number of warm instances.

---

## Evals

```bash
.venv/bin/python evals.py             # terminal pass rate, per split
.venv/bin/python evals_langsmith.py   # dataset sync, experiment, results file
```

Thirteen cases live in `data/golden.json`, in the repository so expectations can be reviewed and
diffed without reading Python. Each case names the guarantee it protects, so a failure reads as a
broken promise instead of a broken node.

### Outcome and trajectory

Every case asserts two things: where the run ended, and which nodes it must never have touched.
The second matters most for an agent holding a payment API. A declined approval asserts that
`execute_payment` never ran. A failed payee check asserts that consent was never queried.

Five evaluators run in LangSmith: `ends_at`, `no_forbidden_nodes`, `vop_status`,
`no_unexpected_pause`, `no_replay`.

### Splits

| Split | Cases | Covers |
|---|---|---|
| `core` | 3 | auto-approval, the ceiling, a human declining |
| `payee-check` | 3 | exact match, near match, uncheckable account |
| `consent` | 2 | expired, missing |
| `input` | 2 | missing account, over-long reference |
| `memory` | 1 | a payee flagged by a human previously |
| `durability` | 1 | resume without replay |
| `adversarial` | 1 | an instruction hidden in the payment reference |

Splits let an experiment show that adversarial went red while core stayed green, which a single
percentage hides.

### What these evals are not

The labels were written alongside the routing code, so they encode the author's intent and not an
independent reviewer's. Every case is synthetic. Nothing has been sampled from real usage. No LLM
sits in the decision path, so nothing here is judged by a model.

---

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .envrc.example .envrc                          # add TrueLayer sandbox credentials
.venv/bin/python scripts/setup_db.py              # once per database
PAYMENTS_MODE=live .venv/bin/python -m uvicorn api.server:app --port 3000
```

`PAYMENTS_MODE=dry` runs every check and skips the payments API call, which is what the evals
use. The port matters only because the return URI you registered has to match.

You need your own TrueLayer sandbox application, your own signing key uploaded to their console,
and your own Postgres. None of those are in this repository.

LangGraph Studio, for the graph and the interrupt visually:

```bash
langgraph dev --port 2024
```

If your LangSmith account is on the EU instance, use the EU Studio host. The US host returns 403
for an EU key with no message explaining why.

---

## Deployment

Vercel with the Python runtime. `api/index.py` imports the same app uvicorn runs locally, so
there is no second code path. Neon is provisioned through the Vercel Marketplace, which injects
the connection strings.

```bash
vercel integration add neon -m region=fra1   # Frankfurt, close to the rail
vercel deploy --prod
```

Production environment variables: `TRUELAYER_CLIENT_ID`, `TRUELAYER_CLIENT_SECRET`,
`TRUELAYER_KID`, `TRUELAYER_PRIVATE_KEY_PEM` (PEM contents, since the deployed filesystem has no
`secrets/`), `PAYMENTS_MODE`, `VOP_PROVIDER`, `RETURN_URI`, `RATE_LIMIT_PER_HOUR`,
`RATE_LIMIT_GLOBAL_PER_DAY`.

---

## Known limits

- Consent and payee history are dicts. Real consent should bind to an OAuth authorisation with a
  real expiry, and payee history belongs in Postgres beside the checkpoints.
- The checkpoint tables are a framework detail and not an audit schema. A payments-grade audit
  trail wants its own append-only table: payment id, payee check result, consent id, risk flags,
  who approved, when, and the ruleset version.
- No model parses the instruction yet, so a hidden instruction in the reference is inert because
  nothing reads it. Adding a parsing node changes that, and the adversarial case starts doing
  real work.
- Neon's free tier suspends when idle, so the first request after a quiet period waits for the
  database to wake.
- The sandbox mock bank is a UK institution, so payments run in GBP over sort code and account
  number rather than SEPA IBANs.

---

## Licence

MIT. See [LICENSE](LICENSE).
