"""Vercel entry point.

The platform imports `app` from this file. Everything else lives in
api/server.py, so the local uvicorn command and the deployed function run
exactly the same application.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.server import app  # noqa: E402,F401
