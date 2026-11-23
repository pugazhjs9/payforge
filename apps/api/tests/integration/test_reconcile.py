from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from payforge.config import settings
from payforge.db import SessionLocal
from payforge.models import (
    Event,
    IdempotencyKey,
    Payment,
    PaymentStatus,
    Refund,
    RefundStatus,
    utcnow,
)
from payforge.services import idempotency, payments, reconcile, refunds
from payforge.services.payments import MerchantRef

from .conftest import MERCHANT_A, merchant, pay, with_key

A = merchant(MERCHANT_A)
pytestmark = pytest.mark.usefixtures("bank")
LONG_AGO = timedelta(minutes=5)


async def make_old(model: type[Payment] | type[Refund], row_id: str) -> None:
    async with SessionLocal.begin() as session:
        await session.execute(
            update(model).where(model.id == row_id).values(created_at=utcnow() - LONG_AGO)
        )


async def payment_status(payment_id: str) -> tuple[PaymentStatus, str | None]:
    async with SessionLocal() as session:
        payment = await session.get_one(Payment, payment_id)
    return payment.status, payment.failure_code


async def test_a_timed_out_payment_the_bank_approved_becomes_succeeded(
    client: httpx.AsyncClient,
) -> None:
    res = await client.post(
        "/v1/payments", json=pay("100.00", "tok_card_slow"), headers=with_key(A)
    )
    assert res.status_code == 202
    await make_old(Payment, res.json()["id"])

    assert await reconcile.reconcile_payments() == 1
    assert await payment_status(res.json()["id"]) == (PaymentStatus.SUCCEEDED, None)
    async with SessionLocal() as session:  # and the merchant hears about it
        assert await session.scalar(select(Event.type)) == "payment.succeeded"


async def test_a_payment_the_bank_never_received_fails() -> None:
    async with SessionLocal.begin() as session:
        payment = await payments.insert_payment(
            session,
            MerchantRef(MERCHANT_A),
            payments.NewPayment(amount="10.00", currency="INR", payment_method="tok_card_ok"),
        )
    await make_old(Payment, payment.id)

    assert await reconcile.reconcile_payments() == 1
    assert await payment_status(payment.id) == (PaymentStatus.FAILED, "not_received_by_bank")


async def test_recent_payments_are_left_alone(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/v1/payments", json=pay("100.00", "tok_card_slow"), headers=with_key(A)
    )

    assert await reconcile.reconcile_payments() == 0
    assert (await payment_status(res.json()["id"]))[0] == PaymentStatus.PROCESSING


async def test_bank_down_leaves_the_payment_processing(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = await client.post(
        "/v1/payments", json=pay("100.00", "tok_card_slow"), headers=with_key(A)
    )
    await make_old(Payment, res.json()["id"])
    monkeypatch.setattr(settings, "simbank_url", "http://127.0.0.1:1")

    assert await reconcile.reconcile_payments() == 0
    assert (await payment_status(res.json()["id"]))[0] == PaymentStatus.PROCESSING


async def test_a_refund_the_bank_never_received_is_sent_again(client: httpx.AsyncClient) -> None:
    paid = await client.post("/v1/payments", json=pay("100.00"), headers=with_key(A))
    async with SessionLocal.begin() as session:  # reserved, but the bank was never called
        refund = await refunds.reserve_refund(
            session, MERCHANT_A, paid.json()["id"], refunds.NewRefund()
        )
    await make_old(Refund, refund.id)

    assert await reconcile.reconcile_refunds() == 1
    async with SessionLocal() as session:
        assert (await session.get_one(Refund, refund.id)).status == RefundStatus.SUCCEEDED


async def test_expired_idempotency_keys_are_deleted_in_batches() -> None:
    for i in range(5):
        claim = await idempotency.claim(MERCHANT_A, f"old-{i}", "h")
        assert isinstance(claim, idempotency.Claim)
    await idempotency.claim(MERCHANT_A, "fresh", "h")
    async with SessionLocal.begin() as session:
        await session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.key.like("old-%"))
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )

    assert await reconcile.delete_expired_idempotency_keys(batch=2) == 5
    async with SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(IdempotencyKey)) == 1
