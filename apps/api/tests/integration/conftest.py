"""Integration tests run against the real MySQL test database (payforge_test)."""

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from payforge.db import engine

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
