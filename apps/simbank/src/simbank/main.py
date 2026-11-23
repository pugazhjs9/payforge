"""Run with:  uv run uvicorn simbank.main:app --port 4000"""

import os

from simbank.app import create_app

app = create_app(
    slow_seconds=float(os.environ.get("SIMBANK_SLOW_SECONDS", "8")),
    upi_seconds=float(os.environ.get("SIMBANK_UPI_SECONDS", "3")),
    webhook_url=os.environ.get("SIMBANK_WEBHOOK_URL", "http://127.0.0.1:3000/webhooks/simbank"),
    webhook_secret=os.environ.get("SIMBANK_WEBHOOK_SECRET", "whsec_simbank_dev"),
)
