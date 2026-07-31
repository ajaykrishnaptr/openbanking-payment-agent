"""The payee history: who this agent has paid, and who a human has flagged.

policy.py holds two kinds of state. Consent is per-user and expires, so it
belongs to whatever issued it. This is the other kind — the long-term half, and
the only reason "this is the first payment to this payee" can mean anything on
the second payment. A module-level set cannot carry it: on Vercel the process
that learns something is gone before the next request arrives.

Selection matches the checkpointer exactly, and for the same reason: Postgres
when a connection string is present, the seed sets otherwise. Local work and
the eval suite therefore need no database and stay deterministic, and the
deployment needs no code change.

Reads fall back to the seeds rather than raising, which is the safe direction.
Losing the history makes a payee look new, a new payee is a risk flag, and a
risk flag means a human decides. A database outage turns auto-approvals into
questions rather than into payments.
"""

from __future__ import annotations

DDL = """
create table if not exists payees (
    name           text primary key,
    status         text not null check (status in ('known', 'flagged')),
    first_seen_at  timestamptz not null default now(),
    last_paid_at   timestamptz,
    payment_count  integer not null default 0,
    flagged_reason text,
    flagged_at     timestamptz
);
"""

# The starting history, and the fallback when there is no database. Kept here
# rather than in policy.py so the rules file stays rules only, and kept
# identical to what the eval suite expects.
SEED_KNOWN = {"Pinguin Pfannkuchen GmbH"}
SEED_FLAGGED = {"Waffelwerk Bremen GmbH"}

SEED_ROWS = (
    [(name, "known") for name in sorted(SEED_KNOWN)]
    + [(name, "flagged") for name in sorted(SEED_FLAGGED)]
)

_ready = False


def _seed_status(payee_name: str) -> str | None:
    if payee_name in SEED_FLAGGED:
        return "flagged"
    if payee_name in SEED_KNOWN:
        return "known"
    return None


def _pool():
    """The shared pool, and the table, or None when there is no database.

    The DDL runs once per process. It is `if not exists` on both the table and
    the seed insert, so several cold serverless instances racing here converge
    on the same state instead of fighting over it.
    """
    global _ready
    from graph.checkpointer import get_pool

    pool = get_pool()
    if pool is None:
        return None
    if not _ready:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(DDL)
            cur.executemany(
                "insert into payees (name, status) values (%s, %s) on conflict (name) do nothing",
                SEED_ROWS,
            )
        _ready = True
    return pool


def status(payee_name: str) -> str | None:
    """"known", "flagged", or None for a payee nobody has seen before."""
    try:
        pool = _pool()
    except Exception as exc:  # noqa: BLE001 — an unreachable database is not a decision
        print(f"[payees] falling back to seeds: {exc}")
        return _seed_status(payee_name)

    if pool is None:
        return _seed_status(payee_name)

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("select status from payees where name = %s", (payee_name,))
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        print(f"[payees] read failed for {payee_name!r}: {exc}")
        return _seed_status(payee_name)

    if row is None:
        return None
    return row["status"] if isinstance(row, dict) else row[0]


def record_payment(payee_name: str) -> bool:
    """Mark a payee as paid. Returns whether it was written; never raises.

    Note what this does to the next run: a payee recorded here is no longer
    first-time, so it stops raising that flag and may auto-approve where it
    previously asked. That is the point of the memory, and it is also a real
    loosening of the rules — graph/app.py only calls this from reconcile(),
    after a payment has actually settled, never on a dry run or a hold.
    """
    pool = None
    try:
        pool = _pool()
        if pool is None:
            return False
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into payees (name, status, last_paid_at, payment_count)
                values (%s, 'known', now(), 1)
                on conflict (name) do update set
                    last_paid_at  = now(),
                    payment_count = payees.payment_count + 1,
                    -- A flag is a human's decision. Paying a flagged payee once
                    -- does not clear it; only clear_flag does.
                    status        = payees.status
                """,
                (payee_name,),
            )
        return True
    except Exception as exc:  # noqa: BLE001 — must not break a payment that already happened
        print(f"[payees] failed to record payment to {payee_name!r}: {exc}")
        return False


def flag(payee_name: str, reason: str) -> bool:
    """Flag a payee so every later payment to it asks a human. Never raises."""
    try:
        pool = _pool()
        if pool is None:
            return False
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into payees (name, status, flagged_reason, flagged_at)
                values (%s, 'flagged', %s, now())
                on conflict (name) do update set
                    status         = 'flagged',
                    flagged_reason = excluded.flagged_reason,
                    flagged_at     = now()
                """,
                (payee_name, reason),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[payees] failed to flag {payee_name!r}: {exc}")
        return False


def clear_flag(payee_name: str) -> bool:
    """Undo a flag, returning the payee to known. Never raises.

    The graph flags a payee whenever a human denies a payment to them, and a
    denial often means "not this amount" rather than "never this payee". This is
    the way back, and it is deliberately not automatic: clearing a flag is a
    loosening, so it takes a person.
    """
    try:
        pool = _pool()
        if pool is None:
            return False
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update payees set status = 'known', flagged_reason = null, flagged_at = null
                where name = %s
                """,
                (payee_name,),
            )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[payees] failed to clear the flag on {payee_name!r}: {exc}")
        return False


def describe() -> str:
    """Where the history is being read from, for the internals panel."""
    try:
        return "postgres" if _pool() is not None else "seed"
    except Exception:  # noqa: BLE001
        return "seed"
