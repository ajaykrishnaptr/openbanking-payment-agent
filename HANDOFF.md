# Handoff — Open Banking payment agent

_Last session: 2026-07-28. Read this first, then README.md for the technical detail._

## Where it stands

Live and working: **https://openbanking-payment-agent.vercel.app**
Repo (public, MIT): **https://github.com/ajaykrishnaptr/openbanking-payment-agent**

An agent that makes account-to-account payments over Open Banking rails on TrueLayer's
sandbox. It checks the payee, checks consent, scores risk, and stops for a human when a rule
says so. Everything below is deployed and verified in production, not just locally.

| Piece | State |
|---|---|
| LangGraph graph, 9 nodes, 10 conditional edges | done |
| `interrupt()` pause + `Command(resume=...)` | done, verified across separate serverless invocations |
| Neon Postgres checkpointer (`eu-central-1`) | done |
| Real TrueLayer sandbox payments + bank SCA + `/callback` | done |
| Semantic payee matching (Claude, behind the VoP adapter) | done, live in production |
| 13-case golden dataset + LangSmith experiments | done, 13/13 |
| LangSmith tracing + per-step timings in the UI | done |
| Append-only audit log + `/activity` page | done |
| Shared rate limiting (Postgres, 20/hr per IP, 400/day) | done |

## The one thing nobody has done

**Complete the mock-bank leg by hand.** Every automated attempt abandoned the TrueLayer hosted
page, so the callback showing a *settled* payment is the only screen never seen. Run one
payment on the live site, click through to the bank, and confirm the callback reports it.

## Next steps, in the order they were agreed

1. **`parse_intent`** — an LLM node at the input edge turning free text into a structured
   instruction, asking rather than inventing when something is missing. This makes the
   `injection-reference` eval case meaningful: today it passes because nothing reads the
   reference field.
2. **Red-team generator** — an agent that invents attack payloads, runs them, and proposes any
   that reach `execute_payment` as new cases in `data/golden.json`.
3. **Production observer** — sample completed runs and check the explanation shown to the user
   against the recorded state. Would have caught the provider-400-shown-as-refusal bug.
4. **Document extraction** — invoice PDF to payment intent, an extension of `parse_intent`.
5. **Learning loop** — capture a decline reason, cluster reasons across declines, propose a new
   rule for `policy.py` that a human accepts or rejects.

## Rules this project holds to

- **The model advises, the rules decide.** `graph/policy.py` owns whether a payment may proceed.
  Any LLM returns a signal. Do not let a model's output be trusted directly, or the claim in the
  README and the article stops being true.
- **Every LLM call goes through `graph/llm.py`** and degrades to deterministic behaviour on any
  failure, timeout, or budget exhaustion.
- **Label what is simulated at the point of use**, not in a footnote. Verification of Payee is
  simulated because TrueLayer's service does not exist for developers yet.
- **The deterministic eval suite gates every change.** Model-dependent evals live separately in
  `evals_semantic.py` because they cost money and vary between runs.
- **No em dashes, no negative parallelism** in any user-facing copy. Openers do not begin with
  "people" and do not centre the author.

## Things that cost time to learn, so do not rediscover them

- **LangSmith EU tenant.** The US host returns 403 for an EU key with no message explaining why.
  `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`, and the EU Studio host for the UI.
- **Neon pooled vs unpooled.** App uses pooled with `prepare_threshold=None` (PgBouncer runs in
  transaction mode). DDL uses the unpooled string. The pool needs
  `check=ConnectionPool.check_connection` or an idle-suspended database hands out dead
  connections.
- **TrueLayer return URIs are exact-match, no wildcards**, and must be registered in the console
  before use. Registered today: `http://localhost:3000/callback` and the production URL. Preview
  deployments work only because `RETURN_URI` is pinned to production.
- **Run locally on port 3000**, because that is the registered local callback.
- **LangSmith indexes a trace over several seconds.** The trace endpoint waits for a terminal
  node before believing a trace is complete, otherwise it caches a partial one.
- **`stream_mode="debug"` gives `task` and `task_result` events**, but at `task_result` time the
  checkpoint is not yet written, so state read there is stale by one node.

## Running it

```bash
cd ~/openbanking-payment-agent
direnv allow                       # or: set -a; . ./.envrc; . ./.env.local; set +a
.venv/bin/python scripts/setup_db.py                    # once per database
PAYMENTS_MODE=live VOP_PROVIDER=semantic \
  .venv/bin/python -m uvicorn api.server:app --port 3000

.venv/bin/python evals.py            # 13 deterministic cases, free
.venv/bin/python evals_langsmith.py  # same cases as a LangSmith experiment
.venv/bin/python evals_semantic.py   # model-dependent, costs money
```

## Publishing

The article draft lives in the session scratchpad, not in this repo. Before it ships it needs one
correction: it says no model sits in the decision path, which was true this morning and is now
half true. The README already states it correctly.
