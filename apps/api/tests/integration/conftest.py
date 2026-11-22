"""Integration tests run against the real MySQL test database (payforge_test)."""

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from payforge.db import engine
from payforge.redis import redis
from simbank.app import BankState
from simbank.app import create_app as create_simbank

from .fake_github import FakeGithub, create_fake_github
from .servers import serve_in_thread

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
async def clean_redis() -> None:
    await redis.flushdb()  # database 1: the test database


@pytest.fixture(autouse=True)
async def clean_db(migrated_db: None) -> None:
    """Empty every table that has rows, then create two merchants.

    DELETE, not TRUNCATE: in MySQL, TRUNCATE drops and recreates the table (slow DDL).
    Only ledger_transfers is truncated, because its append-only trigger blocks DELETE.
    """
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
            has_rows = await conn.scalar(text(f"SELECT EXISTS (SELECT 1 FROM `{table}`)"))
            if not has_rows:
                continue
            verb = "TRUNCATE TABLE" if table == "ledger_transfers" else "DELETE FROM"
            await conn.execute(text(f"{verb} `{table}`"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await conn.execute(
            text("INSERT INTO merchants (id, name) VALUES (:a, 'Merchant A'), (:b, 'Merchant B')"),
            {"a": MERCHANT_A, "b": MERCHANT_B},
        )


@pytest.fixture(scope="session")
def bank() -> Iterator[BankState]:
    """A real SimBank on port 4999. Its slow token (1.5 s) is slower than the API's
    test timeout (0.5 s), on purpose."""
    app = create_simbank(slow_seconds=1.5)
    with serve_in_thread(app, 4999):
        yield app.state.bank


@pytest.fixture(scope="session")
def github_server() -> Iterator[FakeGithub]:
    app, fake = create_fake_github()
    with serve_in_thread(app, 4998):
        yield fake


@pytest.fixture
def github(github_server: FakeGithub) -> FakeGithub:
    """The fake GitHub, reset to its default user for each test."""
    github_server.profile = {"id": 4242, "login": "kiran-dev", "name": "Kiran"}
    github_server.emails = [{"email": "kiran@example.com", "primary": True, "verified": True}]
    return github_server


def merchant(merchant_id: str) -> dict[str, str]:
    """Headers acting as one merchant (temporary X-Merchant-Id identity)."""
    return {"X-Merchant-Id": merchant_id}


def pay(amount: str, payment_method: str = "tok_card_ok", **extra: object) -> dict[str, object]:
    return {"amount": amount, "currency": "INR", "payment_method": payment_method, **extra}
