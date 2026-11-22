"""Integration tests run against the real MySQL test database (payforge_test)."""

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import text

from payforge.db import engine
from simbank.app import BankState
from simbank.app import create_app as create_simbank

API_DIR = Path(__file__).resolve().parents[2]
MERCHANT_A = "mer_a"
MERCHANT_B = "mer_b"


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    """Bring the test database up to the latest migration, once per run."""
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(API_DIR / "alembic.ini"), "upgrade", "head"],
        check=True,
        capture_output=True,
    )


@pytest.fixture(autouse=True)
async def clean_db(migrated_db: None) -> None:
    """Empty every table, then create two merchants."""
    async with engine.begin() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name <> 'alembic_version'"
                )
            )
        ).scalars()
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in list(tables):
            # TRUNCATE (not DELETE) also bypasses the append-only ledger trigger
            await conn.execute(text(f"TRUNCATE TABLE `{table}`"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await conn.execute(
            text("INSERT INTO merchants (id, name) VALUES (:a, 'Merchant A'), (:b, 'Merchant B')"),
            {"a": MERCHANT_A, "b": MERCHANT_B},
        )


@pytest.fixture(scope="session")
def bank() -> Iterator[BankState]:
    """A real SimBank on port 4999, in a background thread. Its slow token (1.5 s) is
    slower than the API's test timeout (0.5 s), on purpose."""
    app = create_simbank(slow_seconds=1.5)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=4999, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("SimBank did not start")
        time.sleep(0.02)
    yield app.state.bank
    server.should_exit = True
    thread.join(timeout=5)


def merchant(merchant_id: str) -> dict[str, str]:
    """Headers acting as one merchant (temporary X-Merchant-Id identity)."""
    return {"X-Merchant-Id": merchant_id}


def pay(amount: str, payment_method: str = "tok_card_ok", **extra: object) -> dict[str, object]:
    return {"amount": amount, "currency": "INR", "payment_method": payment_method, **extra}
