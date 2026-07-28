"""Create the checkpoint tables. Run once per database, not per request.

Uses the NON-pooled connection, because DDL through PgBouncer in transaction
mode is asking for trouble. The app itself uses the pooled URL.

    .venv/bin/python scripts/setup_db.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for env_file in (".env.local", ".env"):
    path = ROOT / env_file
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))

url = os.environ.get("POSTGRES_URL_NON_POOLING") or os.environ.get("DATABASE_URL_UNPOOLED")
if not url:
    raise SystemExit("No unpooled connection string. Run `vercel env pull .env.local` first.")

from langgraph.checkpoint.postgres import PostgresSaver

with PostgresSaver.from_conn_string(url) as saver:
    saver.setup()
print("checkpoint tables ready")

from api.ratelimit import DDL

import psycopg as _psycopg

with _psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
    cur.execute(DDL)
    conn.commit()
print("rate limit table ready")

from graph.audit import DDL as AUDIT_DDL

with _psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
    cur.execute(AUDIT_DDL)
    conn.commit()
print("audit table ready")

import psycopg

with psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
    cur.execute(
        "select table_name from information_schema.tables "
        "where table_schema = 'public' order by table_name"
    )
    for (name,) in cur.fetchall():
        print("  ", name)
