import asyncio

import httpx
import pytest

from payforge.db import SessionLocal
from payforge.models import Payment
from payforge.services.ledger import PLATFORM_FEES, WORLD_BANK, get_balance, merchant_account
from simbank.app import BankState

from .conftest import MERCHANT_A, MERCHANT_B, merchant, pay

A = merchant(MERCHANT_A)
pytestmark = pytest.mark.usefixtures("bank")


async def paid_payment(
    client: httpx.AsyncClient, amount: str = "100.00", token: str = "tok_card_ok"
) -> str:
    res = await client.post("/v1/payments", json=pay(amount, token), headers=A)
    assert res.status_code == 201
    return str(res.json()["id"])


async def test_no_amount_refunds_everything_left(client: httpx.AsyncClient) -> None:
    payment_id = await paid_payment(client, "100.00")
    res = await client.post(f"/v1/payments/{payment_id}/refunds", headers=A)

    assert res.status_code == 201
    assert res.json()["amount"] == "100.00"
    assert res.json()["status"] == "succeeded"
    assert (await client.get(f"/v1/payments/{payment_id}", headers=A)).json()[
        "amount_refunded"
    ] == "100.00"
    # the ₹2 fee is not returned, so the merchant's balance goes negative
    assert (await client.get("/v1/balance", headers=A)).json()["available"] == "-2.00"


async def test_partial_refunds_add_up_and_going_past_the_amount_is_409(
    client: httpx.AsyncClient,
) -> None:
    payment_id = await paid_payment(client, "100.00")

    assert (
        await client.post(f"/v1/payments/{payment_id}/refunds", json={"amount": "30.00"}, headers=A)
    ).status_code == 201
    assert (
        await client.post(f"/v1/payments/{payment_id}/refunds", json={"amount": "50.00"}, headers=A)
    ).status_code == 201
    too_much = await client.post(
        f"/v1/payments/{payment_id}/refunds", json={"amount": "20.01"}, headers=A
    )

    assert too_much.status_code == 409
    assert too_much.json()["error"]["code"] == "refund_exceeds_remaining"
    listed = await client.get(f"/v1/payments/{payment_id}/refunds", headers=A)
    assert len(listed.json()["data"]) == 2


async def test_failed_payment_cannot_be_refunded(client: httpx.AsyncClient) -> None:
    res = await client.post("/v1/payments", json=pay("100.00", "tok_card_declined"), headers=A)
    refund = await client.post(f"/v1/payments/{res.json()['id']}/refunds", headers=A)

    assert refund.status_code == 409
    assert refund.json()["error"]["code"] == "payment_not_refundable"


async def test_racing_refunds_can_never_exceed_the_payment(
    client: httpx.AsyncClient, bank: BankState
) -> None:
    payment_id = await paid_payment(client, "100.00")

    results = await asyncio.gather(
        client.post(f"/v1/payments/{payment_id}/refunds", json={"amount": "60.00"}, headers=A),
        client.post(f"/v1/payments/{payment_id}/refunds", json={"amount": "60.00"}, headers=A),
    )

    assert sorted(r.status_code for r in results) == [201, 409]
    async with SessionLocal() as session:
        payment = await session.get_one(Payment, payment_id)
    assert payment.amount_refunded_minor == 6_000
    bank_refunds = [r for r in bank.refunds.values() if r.charge_reference == payment_id]
    assert len(bank_refunds) == 1  # the bank was asked once


async def test_bank_refusal_gives_the_reserved_amount_back(client: httpx.AsyncClient) -> None:
    payment_id = await paid_payment(client, "100.00", "tok_card_no_refunds")
    res = await client.post(
        f"/v1/payments/{payment_id}/refunds", json={"amount": "40.00"}, headers=A
    )

    assert res.status_code == 201
    assert res.json()["status"] == "failed"
    assert res.json()["failure_code"] == "refund_not_allowed"
    assert (await client.get(f"/v1/payments/{payment_id}", headers=A)).json()[
        "amount_refunded"
    ] == "0.00"


async def test_validation(client: httpx.AsyncClient) -> None:
    payment_id = await paid_payment(client)
    assert (
        await client.post(f"/v1/payments/{payment_id}/refunds", json={"amount": "0.00"}, headers=A)
    ).status_code == 422
    assert (
        await client.post(f"/v1/payments/{payment_id}/refunds", json={"amout": "1.00"}, headers=A)
    ).status_code == 422


async def test_another_merchant_cannot_refund_or_see_refunds(client: httpx.AsyncClient) -> None:
    payment_id = await paid_payment(client)
    other = merchant(MERCHANT_B)
    assert (
        await client.post(f"/v1/payments/{payment_id}/refunds", headers=other)
    ).status_code == 404
    assert (
        await client.get(f"/v1/payments/{payment_id}/refunds", headers=other)
    ).status_code == 404


async def test_ledger_still_balances_after_a_mix(client: httpx.AsyncClient) -> None:
    a = await paid_payment(client, "499.00")
    b = await paid_payment(client, "1250.50")
    await client.post("/v1/payments", json=pay("10.00", "tok_card_declined"), headers=A)
    await client.post(f"/v1/payments/{a}/refunds", json={"amount": "99.99"}, headers=A)
    await client.post(f"/v1/payments/{b}/refunds", headers=A)

    total = sum(
        [
            await get_balance(merchant_account(MERCHANT_A)),
            await get_balance(PLATFORM_FEES),
            await get_balance(WORLD_BANK),
        ]
    )
    assert total == 0
