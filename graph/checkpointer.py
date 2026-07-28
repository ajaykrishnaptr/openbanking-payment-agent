"""Where a paused run is kept.

An interrupt is only useful if the paused state outlives the request that
created it. On one long-lived server, memory is enough. On Vercel, the process
is gone before the human decides, so the checkpoint has to live in Postgres.

Selection is by environment, so local development and the eval suite keep using
memory with no configuration, and the deployed app uses the database.
"""

from __future__ import annotations

import os

from langgraph.checkpoint.memory import InMemorySaver

_pool = None


def _postgres_saver(url: str):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from langgraph.checkpoint.postgres import PostgresSaver

    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=url,
            min_size=0,          # a cold serverless instance should not pre-open
            max_size=5,
            open=True,
            # Neon suspends an idle database and drops the connections with it.
            # Without these the pool hands out a dead connection and the request
            # fails with "server closed the connection unexpectedly".
            check=ConnectionPool.check_connection,  # validate on checkout
            max_idle=60,                            # retire idle connections first
            max_lifetime=600,                       # and recycle live ones hourly
            reconnect_timeout=15,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                # Neon's pooled endpoint is PgBouncer in transaction mode, which
                # cannot carry psycopg's server-side prepared statements between
                # transactions. Without this the second query on a pooled
                # connection fails.
                "prepare_threshold": None,
            },
        )
    return PostgresSaver(_pool)


def get_pool():
    """The connection pool, once one exists. The rate limiter shares it rather
    than opening a second set of connections to the same database."""
    url = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if not url:
        return None
    if _pool is None:
        _postgres_saver(url)
    return _pool


def get_checkpointer():
    """Postgres when a connection string is present, memory otherwise."""
    url = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    if url:
        return _postgres_saver(url)
    return InMemorySaver()


def describe() -> str:
    url = os.getenv("POSTGRES_URL") or os.getenv("DATABASE_URL")
    return "postgres" if url else "memory"
