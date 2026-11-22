"""Test settings. They are applied before the app is imported, so tests never
touch the development database."""

import os

TEST_ENV = {
    "ENVIRONMENT": "test",
    "LOG_LEVEL": "silent",
    "DATABASE_URL": "mysql+asyncmy://payforge:payforge@127.0.0.1:3307/payforge_test",
    "SIMBANK_URL": "http://127.0.0.1:4999",  # tests start their own SimBank on this port
    "SIMBANK_TIMEOUT_SECONDS": "0.5",
    "REDIS_URL": "redis://127.0.0.1:6381/1",  # database 1: never the dev data in database 0
}
os.environ.update(TEST_ENV)

from collections.abc import AsyncIterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from payforge.main import create_app  # noqa: E402


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
