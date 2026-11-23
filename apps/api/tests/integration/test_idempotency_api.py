"""Idempotency-Key on POST /v1/payments and POST /v1/payments/{id}/refunds."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from payforge.api.v1.schemas import CreatePayment
from payforge.clients import simbank
from payforge.db import SessionLocal
from payforge.models import IdempotencyKey, Payment, Refund, utcnow
from payforge.services import idempotency, payments
from payforge.services.payments import MerchantRef

from .conftest import MERCHANT_A, MERCHANT_B, merchant, pay, with_key

A = merchant(MERCHANT_A)
pytestmark = pytest.mark.usefixtures("bank")


async def count(model: type) -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def create(
    client: httpx.AsyncClient, key: str, body: dict[str, object], headers: dict[str, str] = A
) -> httpx.Response:
    return await client.post("/v1/payments", json=body, headers=with_key(headers, key))


# ── the header ────────────────────────────────────────────────────────────────


async def test_no_key_is_400_on_payments_and_refunds(client: httpx.AsyncClient) -> None:
    res = await client.post("/v1/payments", json=pay("10.00"), headers=A)
    refund = await client.post("/v1/payments/pay_x/refunds", headers=A)

    assert res.status_code == refund.status_code == 400
    assert res.json()["error"]["code"] == "idempotency_key_missing"
    assert await count(Payment) == 0


@pytest.mark.parametrize("key", [b"has space", b"x" * 256, "ключ".encode()])
async def test_badly_formed_key_is_422(client: httpx.AsyncClient, key: bytes) -> None:
    res = await client.post(
        "/v1/payments", json=pay("10.00"), headers=[*A.items(), ("Idempotency-Key", key)]
    )
    assert res.status_code == 422
    assert res.json()["error"]["details"][0]["field"] == "Idempotency-Key"


# ── retries ───────────────────────────────────────────────────────────────────


async def test_retry_returns_the_first_response_and_charges_once(
    client: httpx.AsyncClient,
) -> None:
    first = await create(client, "order-1042", pay("499.00"))
    retry = await create(client, "order-1042", pay("499.00"))

    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert await count(Payment) == 1
    balance = await client.get("/v1/balance", headers=A)
    assert balance.json()["available"] == "489.02"  # credited once


async def test_same_key_with_a_different_body_is_422(client: httpx.AsyncClient) -> None:
    await create(client, "order-1042", pay("499.00"))
    res = await create(client, "order-1042", pay("500.00"))

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "idempotency_key_reused"
    assert await count(Payment) == 1


async def test_a_202_is_replayed_as_202(client: httpx.AsyncClient) -> None:
    first = await create(client, "slow-1", pay("100.00", "tok_card_slow"))
    retry = await create(client, "slow-1", pay("100.00", "tok_card_slow"))

    assert first.status_code == retry.status_code == 202
    assert retry.json()["id"] == first.json()["id"]


async def test_the_same_key_from_two_merchants_is_two_payments(client: httpx.AsyncClient) -> None:
    a = await create(client, "order-1", pay("10.00"))
    b = await create(client, "order-1", pay("10.00"), merchant(MERCHANT_B))

    assert a.json()["id"] != b.json()["id"]
    assert "idempotent-replayed" not in b.headers


async def test_responses_keep_their_rate_limit_headers(client: httpx.AsyncClient) -> None:
    first = await create(client, "order-1", pay("10.00"))
    retry = await create(client, "order-1", pay("10.00"))

    assert "ratelimit-remaining" in first.headers
    assert "ratelimit-remaining" in retry.headers


async def test_fifty_parallel_retries_create_one_payment(client: httpx.AsyncClient) -> None:
    responses = await asyncio.gather(
        *(create(client, "double-click", pay("100.00")) for _ in range(50))
    )

    statuses = [r.status_code for r in responses]
    assert set(statuses) <= {201, 409}
    assert statuses.count(201) >= 1
    assert len({r.json()["id"] for r in responses if r.status_code == 201}) == 1
    assert all(r.headers["retry-after"] == "1" for r in responses if r.status_code == 409)
    assert await count(Payment) == 1


# ── failures and crashes ──────────────────────────────────────────────────────


async def test_a_rejected_request_does_not_burn_the_key(client: httpx.AsyncClient) -> None:
    """Nothing was created, so the key is forgotten and the fixed request can use it."""
    too_small = await create(client, "order-1", pay("0.50"))
    fixed = await create(client, "order-1", pay("5.00"))

    assert too_small.status_code == 422
    assert fixed.status_code == 201
    assert "idempotent-replayed" not in fixed.headers


async def test_a_crash_after_creating_the_payment_resumes_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_bank_client(**_kwargs: object) -> None:
        raise RuntimeError("bug in the bank client")

    monkeypatch.setattr(simbank, "charge", broken_bank_client)
    crashed = await create(client, "order-1", pay("100.00"))
    monkeypatch.undo()
    retry = await create(client, "order-1", pay("100.00"))

    assert crashed.status_code == 500
    assert retry.status_code == 201
    assert retry.json()["status"] == "succeeded"
    assert await count(Payment) == 1  # the same payment, finished, not a second one


async def test_a_dead_workers_stale_lock_is_taken_over(client: httpx.AsyncClient) -> None:
    """Simulates a process that died mid-request: payment created and linked, then
    nothing. After the lock times out, a retry continues with that payment."""
    body = pay("100.00")
    request_hash = idempotency.fingerprint(
        "POST", "/v1/payments", CreatePayment.model_validate(body).model_dump(mode="json")
    )
    claim = await idempotency.claim(MERCHANT_A, "order-1", request_hash)
    assert isinstance(claim, idempotency.Claim)
    async with SessionLocal.begin() as session:
        payment = await payments.insert_payment(
            session,
            MerchantRef(MERCHANT_A),
            payments.NewPayment(amount="100.00", currency="INR", payment_method="tok_card_ok"),
        )
        await idempotency.link(session, claim, payment.id)

    assert (await create(client, "order-1", body)).status_code == 409  # lock still fresh

    async with SessionLocal.begin() as session:
        await session.execute(
            update(IdempotencyKey).values(locked_at=utcnow() - timedelta(minutes=2))
        )
    retry = await create(client, "order-1", body)

    assert retry.status_code == 201
    assert retry.json()["id"] == payment.id
    assert retry.json()["status"] == "succeeded"
    assert await count(Payment) == 1


# ── refunds ───────────────────────────────────────────────────────────────────


async def test_refund_retried_with_the_same_key_refunds_once(client: httpx.AsyncClient) -> None:
    payment_id = (await create(client, "order-1", pay("100.00"))).json()["id"]
    path = f"/v1/payments/{payment_id}/refunds"

    first = await client.post(path, json={"amount": "30.00"}, headers=with_key(A, "refund-1"))
    retry = await client.post(path, json={"amount": "30.00"}, headers=with_key(A, "refund-1"))

    assert first.status_code == retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert await count(Refund) == 1
    payment = await client.get(f"/v1/payments/{payment_id}", headers=A)
    assert payment.json()["amount_refunded"] == "30.00"


async def test_a_refused_refund_does_not_burn_the_key(client: httpx.AsyncClient) -> None:
    payment_id = (await create(client, "order-1", pay("100.00"))).json()["id"]
    path = f"/v1/payments/{payment_id}/refunds"

    too_much = await client.post(path, json={"amount": "200.00"}, headers=with_key(A, "r-1"))
    fixed = await client.post(path, json={"amount": "50.00"}, headers=with_key(A, "r-1"))

    assert too_much.status_code == 409
    assert fixed.status_code == 201


async def test_the_same_key_on_another_payments_refund_is_422(client: httpx.AsyncClient) -> None:
    one = (await create(client, "order-1", pay("100.00"))).json()["id"]
    two = (await create(client, "order-2", pay("100.00"))).json()["id"]

    await client.post(f"/v1/payments/{one}/refunds", headers=with_key(A, "r-1"))
    res = await client.post(f"/v1/payments/{two}/refunds", headers=with_key(A, "r-1"))

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "idempotency_key_reused"
