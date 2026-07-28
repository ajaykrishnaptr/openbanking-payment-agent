"""The audit record.

Distinct from the checkpoints, deliberately. Checkpoints exist so a paused run
can resume; they are keyed by thread id, hold framework-shaped state, and
LangGraph may change their schema whenever it likes. Neither property is
acceptable for a record you might have to produce months later.

This table is the opposite: one row per completed run, written once, never
updated, with the columns a reviewer would actually ask for. It is queryable
by payee, amount, outcome and date without deserialising anything.

Nothing here is on the decision path. A failure to write an audit row is
logged and swallowed, because losing the record of a payment is bad and
failing the payment because the record could not be written is worse.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# Bump when the rules in policy.py change, so an old decision can be read
# against the rules that were actually in force when it was made.
RULESET_VERSION = "2026-07-28.1"

DDL = """
create table if not exists audit_log (
    id              bigserial primary key,
    recorded_at     timestamptz not null default now(),
    thread_id       text not null,
    outcome         text not null,
    payee_name      text not null,
    account_last4   text,
    amount_minor    bigint,
    currency        text,
    reference       text,
    vop_status      text,
    vop_provider    text,
    vop_confidence  numeric,
    vop_reason      text,
    consent_id      text,
    consent_status  text,
    risk_flags      jsonb,
    decided_by      text,
    decision        text,
    payment_id      text,
    payment_status  text,
    route           jsonb,
    ruleset_version text not null
);
create index if not exists audit_log_recorded_at on audit_log (recorded_at desc);
create index if not exists audit_log_payee on audit_log (payee_name);
create index if not exists audit_log_payment on audit_log (payment_id);
"""

INSERT = """
insert into audit_log (
    thread_id, outcome, payee_name, account_last4, amount_minor, currency, reference,
    vop_status, vop_provider, vop_confidence, vop_reason,
    consent_id, consent_status, risk_flags,
    decided_by, decision, payment_id, payment_status, route, ruleset_version
) values (
    %(thread_id)s, %(outcome)s, %(payee_name)s, %(account_last4)s, %(amount_minor)s, %(currency)s, %(reference)s,
    %(vop_status)s, %(vop_provider)s, %(vop_confidence)s, %(vop_reason)s,
    %(consent_id)s, %(consent_status)s, %(risk_flags)s,
    %(decided_by)s, %(decision)s, %(payment_id)s, %(payment_status)s, %(route)s, %(ruleset_version)s
)
"""


def row_from_state(thread_id: str, state: dict, decided_by: str | None) -> dict:
    vop = state.get("vop") or {}
    consent = state.get("consent") or {}
    execution = state.get("execution") or {}
    account = state.get("account") or {}
    number = str(account.get("account_number") or "")

    return {
        "thread_id": thread_id,
        "outcome": state.get("outcome") or "completed",
        "payee_name": state.get("payee_name") or "",
        # Never store a full account number in a record that outlives the run.
        "account_last4": number[-4:] if number else None,
        "amount_minor": state.get("amount_minor"),
        "currency": state.get("currency"),
        "reference": state.get("reference"),
        "vop_status": vop.get("status"),
        "vop_provider": vop.get("provider"),
        "vop_confidence": vop.get("confidence"),
        "vop_reason": vop.get("reason"),
        "consent_id": consent.get("consent_id"),
        "consent_status": consent.get("status"),
        "risk_flags": json.dumps(state.get("risk_flags") or []),
        # "policy" means the rules allowed it without asking anyone.
        "decided_by": decided_by or ("policy" if "human_approval" not in (state.get("trail") or []) else "human"),
        "decision": state.get("human_decision"),
        "payment_id": execution.get("payment_id"),
        "payment_status": execution.get("settled_status") or execution.get("status"),
        "route": json.dumps(state.get("trail") or []),
        "ruleset_version": RULESET_VERSION,
    }


def record(thread_id: str, state: dict, decided_by: str | None = None) -> bool:
    """Write one row. Returns whether it was written; never raises."""
    from graph.checkpointer import get_pool

    pool = get_pool()
    if pool is None:
        return False

    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(INSERT, row_from_state(thread_id, state, decided_by))
        return True
    except Exception as exc:  # noqa: BLE001 — the record must not break the payment
        print(f"[audit] failed to record {thread_id}: {exc}")
        return False


def recent(limit: int = 25) -> list[dict]:
    """The activity list, newest first."""
    from graph.checkpointer import get_pool

    pool = get_pool()
    if pool is None:
        return []

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select recorded_at, outcome, payee_name, account_last4, amount_minor, currency,
                   vop_status, vop_provider, vop_confidence, consent_status, risk_flags,
                   decided_by, decision, payment_id, payment_status, route, ruleset_version
            from audit_log order by recorded_at desc limit %s
            """,
            (limit,),
        )
        rows = cur.fetchall()

    out = []
    for row in rows:
        record_ = dict(row) if isinstance(row, dict) else row
        if isinstance(record_.get("recorded_at"), datetime):
            record_["recorded_at"] = record_["recorded_at"].astimezone(timezone.utc).isoformat()
        if record_.get("vop_confidence") is not None:
            record_["vop_confidence"] = float(record_["vop_confidence"])
        out.append(record_)
    return out


def summary() -> dict:
    """Counts a reviewer would ask for first."""
    from graph.checkpointer import get_pool

    pool = get_pool()
    if pool is None:
        return {}

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select count(*) total,
                   count(*) filter (where payment_id is not null) paid,
                   count(*) filter (where decided_by = 'human') human_decided,
                   count(*) filter (where outcome like 'held%%') held,
                   count(*) filter (where amount_minor > %s) above_ceiling
            from audit_log
            """,
            (int(os.getenv("AUDIT_CEILING_MINOR", "100000")),),
        )
        row = cur.fetchone()
    return dict(row) if isinstance(row, dict) else {}
