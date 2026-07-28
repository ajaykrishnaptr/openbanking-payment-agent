"""Two-layer rate limiting, shared across instances.

Ported from the agent-payment-authority prototype, which put the same design on
Upstash. Here it runs on the Postgres that already holds the checkpoints, so
there is one store instead of two and the counter is atomic, which the KV
version could not promise.

  per IP     stops one visitor hammering the sandbox
  global     a hard daily cap, so total sandbox volume is bounded no matter how
             many addresses show up

A module-level dict cannot do this job on Vercel: each instance keeps its own
counters, so the real limit is the stated one multiplied by the number of warm
instances.
"""

from __future__ import annotations

import os
import time

DDL = """
create table if not exists rate_limits (
    bucket text primary key,
    hits integer not null default 0,
    expires_at bigint not null
)
"""


def _pool():
    from graph.checkpointer import get_pool

    return get_pool()


def _hit(bucket: str, limit: int, window_s: int) -> tuple[bool, int]:
    """Count one hit in the current window. Returns (allowed, remaining).

    The insert and increment happen in one statement, so two concurrent
    requests cannot both read the same count and both decide they are fine.
    """
    now = int(time.time())
    key = f"{bucket}:{now // window_s}"
    expires = (now // window_s + 1) * window_s

    pool = _pool()
    if pool is None:
        return True, limit  # no database locally, so no shared limit to enforce

    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into rate_limits (bucket, hits, expires_at)
            values (%s, 1, %s)
            on conflict (bucket) do update set hits = rate_limits.hits + 1
            returning hits
            """,
            (key, expires),
        )
        row = cur.fetchone()
        hits = row["hits"] if isinstance(row, dict) else row[0]

        # Opportunistic cleanup, cheap and keeps the table from growing.
        if hits % 50 == 0:
            cur.execute("delete from rate_limits where expires_at < %s", (now,))

    return hits <= limit, max(0, limit - hits)


PER_IP_LIMIT = int(os.getenv("RATE_LIMIT_PER_HOUR", "20"))
GLOBAL_DAILY_LIMIT = int(os.getenv("RATE_LIMIT_GLOBAL_PER_DAY", "400"))


def check(ip: str) -> tuple[bool, str]:
    """Returns (allowed, message). The message explains the refusal in words a
    visitor can act on."""
    allowed, remaining = _hit(f"ip:{ip}", PER_IP_LIMIT, 3600)
    if not allowed:
        return False, (
            f"That is {PER_IP_LIMIT} runs from this address in an hour, which is the cap on this "
            "demo. The sandbox is real infrastructure and this keeps it usable for everyone. "
            "Try again shortly."
        )

    allowed, _ = _hit("global:day", GLOBAL_DAILY_LIMIT, 86_400)
    if not allowed:
        return False, (
            f"This demo has run {GLOBAL_DAILY_LIMIT} payments today, which is its daily cap. "
            "The graph, the rules and the eval suite are all readable in the repository in the "
            "meantime."
        )

    return True, ""
