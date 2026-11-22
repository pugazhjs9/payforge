"""Run with:  uv run uvicorn simbank.main:app --port 4000"""

import os

from simbank.app import create_app

app = create_app(slow_seconds=float(os.environ.get("SIMBANK_SLOW_SECONDS", "8")))
