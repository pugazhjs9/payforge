"""Integration tests run against the real MySQL test database (payforge_test)."""

import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from aio_pika.abc import AbstractChannel, AbstractRobustConnection
from sqlalchemy import text

from payforge import messaging, storage
from payforge.auth.tokens import issue_access_token
from payforge.db import engine
from payforge.redis import redis
from simbank.app import BankState
from simbank.app import create_app as create_simbank

from .fake_github import FakeGithub, create_fake_github
from .fake_merchant import FakeMerchant, create_fake_merchant
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


RABBITMQ_ADMIN = "http://127.0.0.1:15673/api"
TEST_VHOST = "payforge_test"


@pytest.fixture(scope="session")
async def amqp() -> AsyncIterator[AbstractRobustConnection]:
    """A RabbitMQ connection to a vhost of its own, created through the management API."""
    auth = ("guest", "guest")
    async with httpx.AsyncClient(auth=auth) as admin:
        (await admin.put(f"{RABBITMQ_ADMIN}/vhosts/{TEST_VHOST}")).raise_for_status()
        (
            await admin.put(
                f"{RABBITMQ_ADMIN}/permissions/{TEST_VHOST}/guest",
                json={"configure": ".*", "write": ".*", "read": ".*"},
            )
        ).raise_for_status()
    connection = await messaging.connect()
    yield connection
    await connection.close()


@pytest.fixture
async def channel(amqp: AbstractRobustConnection) -> AsyncIterator[AbstractChannel]:
    """A channel with every exchange and queue declared, and every queue empty."""
    ch = await amqp.channel(on_return_raises=True)
    await messaging.declare(ch)
    for name in [
        messaging.FANOUT_QUEUE,
        messaging.DELIVER_QUEUE,
        messaging.DEAD_QUEUE,
        messaging.JOBS_QUEUE,
        *map(messaging.retry_queue, messaging.RETRY_DELAYS),
    ]:
        await (await ch.get_queue(name)).purge()
    yield ch
    await ch.close()


@pytest.fixture(scope="session")
async def s3() -> None:
    """The test bucket on the local S3 server (SeaweedFS)."""
    await storage.ensure_bucket()


@pytest.fixture(scope="session")
def bank() -> Iterator[BankState]:
    """A real SimBank on port 4999. Its slow token (1.5 s) is slower than the API's
    test timeout (0.5 s), on purpose. UPI payments settle after 0.2 s; their webhooks
    are recorded in bank.webhooks, and tests hand them to the API themselves."""
    app = create_simbank(
        slow_seconds=1.5, upi_seconds=0.2, webhook_url=None, webhook_secret="whsec_simbank_test"
    )
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


MERCHANT_SITE = "http://127.0.0.1:4996"


@pytest.fixture(scope="session")
def merchant_site_server() -> Iterator[FakeMerchant]:
    app, fake = create_fake_merchant()
    with serve_in_thread(app, 4996):
        yield fake


@pytest.fixture
def merchant_site(merchant_site_server: FakeMerchant) -> FakeMerchant:
    """A merchant's webhook receiver on MERCHANT_SITE, emptied for each test."""
    merchant_site_server.reset()
    return merchant_site_server


ALL_SCOPES = ["payments:read", "payments:write", "refunds:write"]


def merchant(merchant_id: str, scopes: list[str] | None = None) -> dict[str, str]:
    """Headers acting as one merchant: a valid access token, as if from POST /v1/auth/token."""
    token = issue_access_token(
        merchant_id=merchant_id, key_id=f"key_test_{merchant_id}", scopes=scopes or ALL_SCOPES
    )
    return {"Authorization": f"Bearer {token}"}


def pay(amount: str, payment_method: str = "tok_card_ok", **extra: object) -> dict[str, object]:
    return {"amount": amount, "currency": "INR", "payment_method": payment_method, **extra}


def with_key(headers: dict[str, str], key: str | None = None) -> dict[str, str]:
    """Adds an Idempotency-Key (a fresh one unless given): required on every create."""
    return {**headers, "Idempotency-Key": key or f"test-{uuid.uuid4()}"}
