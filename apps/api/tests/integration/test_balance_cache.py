import httpx
import pytest

from payforge.db import SessionLocal
from payforge.redis import redis
from payforge.services.ledger import balance_cache_key, payment_transfers, post_transaction

from .conftest import MERCHANT_A, MERCHANT_B, merchant, pay

A = merchant(MERCHANT_A)
pytestmark = pytest.mark.usefixtures("bank")


async def balance(client: httpx.AsyncClient, headers: dict[str, str] = A) -> str:
    res = await client.get("/v1/balance", headers=headers)
    assert res.status_code == 200
    return str(res.json()["available"])


async def test_balance_is_served_from_the_cache(client: httpx.AsyncClient) -> None:
    assert await balance(client) == "0.00"
    # write to the ledger WITHOUT invalidating: the cached value is still returned
    async with SessionLocal.begin() as session:
        await post_transaction(
            session,
            key="test:1",
            merchant_id=MERCHANT_A,
            transfers=payment_transfers(MERCHANT_A, 10_000, 200),
        )

    assert await balance(client) == "0.00"
    await redis.delete(balance_cache_key(MERCHANT_A))
    assert await balance(client) == "98.00"


async def test_a_succeeded_payment_refreshes_the_cached_balance(
    client: httpx.AsyncClient,
) -> None:
    assert await balance(client) == "0.00"  # now cached
    await client.post("/v1/payments", json=pay("100.00"), headers=A)

    assert await balance(client) == "98.00"


async def test_a_succeeded_refund_refreshes_the_cached_balance(client: httpx.AsyncClient) -> None:
    payment = await client.post("/v1/payments", json=pay("100.00"), headers=A)
    assert await balance(client) == "98.00"  # now cached
    await client.post(
        f"/v1/payments/{payment.json()['id']}/refunds", json={"amount": "40.00"}, headers=A
    )

    assert await balance(client) == "58.00"


async def test_cache_keys_are_per_merchant(client: httpx.AsyncClient) -> None:
    await client.post("/v1/payments", json=pay("100.00"), headers=A)

    assert await balance(client) == "98.00"
    assert await balance(client, merchant(MERCHANT_B)) == "0.00"
