# Open Banking payment agent

**Live demo: https://openbanking-payment-agent.vercel.app**

An agent that will not move money on its own. It reads a payment instruction, checks the payee
name against the account, checks the consent is still valid, scores the risk, and then stops
and explains why it stopped. A human decides. Only then does it create a payment, and the bank
still asks that human to authenticate, because that step belongs to a person.

Built with Claude Code: every product, architecture and routing decision is mine, the
implementation was written under my direction.

## What is real, and what is not

| | |
|---|---|
| **Real** | OAuth client credentials, request signing, `POST /v3/payments`, payment status read back from the provider, the bank's own authentication page |
| **Simulated** | Verification of Payee. TrueLayer's VoP service is expected H2 2026 and is not available on a developer account. Checked directly: the `verification` scope resolves to `data_api`, and payee-verification paths on the Payments API return 404. It runs behind an adapter (`graph/vop.py`), so the real service replaces one implementation |
| **Also simulated** | Consent records and payee history, which are dicts until Postgres |
| **Never real** | The money. TrueLayer sandbox only |

## The graph

```
check_input ─┬─ need_more_info                        (something required is missing)
             └─ verify_payee ─┬─ hold_or_reject       (NO_MATCH, MATCH_NOT_POSSIBLE)
                              └─ check_consent ─┬─ hold_or_reject   (expired, missing)
                                                └─ assess_risk ─┬─ execute_payment   (clean, under ceiling)
                                                                └─ human_approval ─┬─ execute_payment
                                                                                   └─ hold_or_reject
execute_payment → reconcile → END
```

Two locks on money movement: the edges, and a guard inside `execute_payment` that raises if it
is reached without an approval or a clean auto-approve. The second one exists so a miswired
edge cannot pay anyone.

## Evals

```bash
.venv/bin/python evals.py             # 12 scenarios, terminal pass rate
.venv/bin/python evals_langsmith.py   # same scenarios as a LangSmith dataset + experiment
```

Each scenario asserts an outcome (where it ended) and a trajectory (which nodes it must never
have touched). The trajectory half is the one that matters: "did `execute_payment` ever run"
is the question worth asking of an agent that spends money. Five evaluators run in LangSmith:
`ends_at`, `no_forbidden_nodes`, `vop_status`, `no_unexpected_pause`, `no_replay`.

Scenarios include a wrong payee, an uncheckable account, expired consent, missing consent, a
flagged payee, an amount above the ceiling, a human declining, missing input, an instruction
hidden in the payment reference, and a resume that must not replay earlier steps.

## Run it locally

```bash
cp .envrc.example .envrc            # add TrueLayer sandbox credentials, then: direnv allow
.venv/bin/pip install -r requirements.txt
PAYMENTS_MODE=live .venv/bin/python -m uvicorn api.server:app --port 3000
```

`PAYMENTS_MODE=dry` runs everything except the payments API call, which is what the evals use.

LangGraph Studio, for the visual graph and the interrupt:

```bash
langgraph dev --port 2024
# then https://eu.smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
```

The EU Studio host matters if your LangSmith account is on the EU instance. The US host returns
403 for an EU key, with no message saying why.

## The callback, and why it needs attention before deploying

After a payment is created, TrueLayer sends the payer to the bank, and the bank returns them to
`return_uri`. That URI is worked out per request:

1. `RETURN_URI`, if set, wins.
2. Otherwise `x-forwarded-host` and `x-forwarded-proto` are used, which is what a Vercel
   deployment sends, giving `https://<your-domain>/callback`.
3. Otherwise the request's own origin, which covers local development on any port.

**Every value must be registered in the TrueLayer console first.** An unregistered URI fails the
payment outright with HTTP 400 and `"Return URI must be added in the TrueLayer Console before
use"`. This bites twice: `http://localhost:3000/callback` for local work, and the production
domain for the deployment. Vercel preview URLs change per deployment, so either register a stable
production domain and pin `RETURN_URI` to it, or accept that previews cannot complete a payment.

The callback page reads the outcome from the provider rather than from local state, so it works
on serverless without a database. It handles three endings: a completed payment, a failure, and
`tl_hpp_abandoned`, which is TrueLayer's way of saying the payer closed the bank screen without
authenticating. That last one is the escape hatch working rather than an error, and it is worded
that way.

## Persistence

`graph/checkpointer.py` picks the checkpointer by environment: Postgres when `POSTGRES_URL` is
set, memory otherwise. Local work and the eval suite need no configuration; the deployed app gets
durable pauses.

The database is Neon, provisioned through the Vercel Marketplace and connected to the project, in
`eu-central-1` so it is close to the payment rail. Two details that will bite otherwise:

- The app uses the **pooled** connection string. Neon's pooler is PgBouncer in transaction mode,
  which cannot carry psycopg's server-side prepared statements between transactions, so the pool
  is opened with `prepare_threshold=None`.
- `scripts/setup_db.py` creates the checkpoint tables and uses the **unpooled** string, because
  DDL through a transaction-mode pooler is asking for trouble. Run it once per database.
- Neon **suspends an idle database** and drops its connections. A pool that keeps handing out
  those connections fails with `consuming input failed: server closed the connection
  unexpectedly`. The pool therefore validates on checkout (`check=ConnectionPool.check_connection`)
  and recycles connections before Neon can kill them (`max_idle=60`, `max_lifetime=600`).

Verified rather than assumed: a run paused in one OS process resumes in a separate process with
no replay of earlier nodes, and the same holds over HTTP across two requests.

## Still to do before deploying

`RATE_LIMIT_PER_HOUR` is a module-level dict, so the cap is per instance. With five warm instances
the effective limit is five times what it claims. The `store.py` and `ratelimit.py` pair from the
agent-payment-authority prototype already solves this against Upstash and should be ported.
