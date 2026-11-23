import asyncio

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from payforge.db import SessionLocal
from payforge.models import Event, LedgerTransaction, Payment, PaymentStatus
from payforge.services.payments import PaymentFailed, PaymentSucceeded, finalize_payment

from .conftest import MERCHANT_A, merchant, pay, with_key

A = merchant(MERCHANT_A)
pytestmark = pytest.mark.usefixtures("bank")


async def all_events() -> list[Event]:
    async with SessionLocal() as session:
        return list(await session.scalars(select(Event).order_by(Event.created_at, Event.id)))


async def create(client: httpx.AsyncClient, token: str = "tok_card_ok") -> httpx.Response:
    return await client.post("/v1/payments", json=pay("100.00", token), headers=with_key(A))


async def test_a_succeeded_payment_records_an_event_with_its_snapshot(
    client: httpx.AsyncClient,
) -> None:
    res = await create(client)

    [event] = await all_events()
    assert event.type == "payment.succeeded"
    assert event.resource_id == res.json()["id"]
    assert event.merchant_id == MERCHANT_A
    assert event.data == res.json()  # exactly what the API returned
    assert event.published_at is None  # waiting for the relay


async def test_a_declined_payment_records_payment_failed(client: httpx.AsyncClient) -> None:
    await create(client, "tok_card_declined")

    assert [e.type for e in await all_events()] == ["payment.failed"]


async def test_no_event_while_processing_then_one_when_it_finishes(
    client: httpx.AsyncClient,
) -> None:
    res = await create(client, "tok_card_slow")
    assert await all_events() == []

    await finalize_payment(res.json()["id"], PaymentSucceeded())
    assert [e.type for e in await all_events()] == ["payment.succeeded"]


async def test_racing_finalizers_record_exactly_one_event(client: httpx.AsyncClient) -> None:
    payment_id = (await create(client, "tok_card_slow")).json()["id"]

    await asyncio.gather(
        finalize_payment(payment_id, PaymentSucceeded()),
        finalize_payment(payment_id, PaymentFailed(failure_code="expired")),
        finalize_payment(payment_id, PaymentSucceeded()),
    )
    assert len(await all_events()) == 1


async def test_event_and_status_change_commit_together_or_not_at_all(
    client: httpx.AsyncClient,
) -> None:
    payment_id = (await create(client, "tok_card_slow")).json()["id"]
    async with SessionLocal.begin() as session:  # make the ledger posting fail
        session.add(
            LedgerTransaction(id="ltx_x", transaction_key=f"payment:{payment_id}", merchant_id="m")
        )

    with pytest.raises(IntegrityError):
        await finalize_payment(payment_id, PaymentSucceeded())

    async with SessionLocal() as session:
        payment = await session.get_one(Payment, payment_id)
    assert payment.status == PaymentStatus.PROCESSING
    assert await all_events() == []


async def test_refund_events(client: httpx.AsyncClient) -> None:
    ok = (await create(client)).json()["id"]
    no_refunds = (await create(client, "tok_card_no_refunds")).json()["id"]
    await client.post(f"/v1/payments/{ok}/refunds", headers=with_key(A))
    await client.post(f"/v1/payments/{no_refunds}/refunds", headers=with_key(A))

    types = [e.type for e in await all_events()]
    assert types[2:] == ["refund.succeeded", "refund.failed"]
