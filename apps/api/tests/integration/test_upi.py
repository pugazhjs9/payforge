"""UPI: the bank answers "pending", then tells us the result by webhook."""

import asyncio
import json
import time

import httpx
import pytest
from sqlalchemy import func, select, update

from payforge.db import SessionLocal
from payforge.models import Event, InboundWebhook, Payment, utcnow
from payforge.services import reconcile
from payforge.services.reconcile import STUCK_AFTER
from simbank.app import BankState, SentWebhook, sign

from .conftest import MERCHANT_A, merchant, pay, with_key

A = merchant(MERCHANT_A)
SECRET = "whsec_simbank_test"
pytestmark = pytest.mark.usefixtures("bank")


async def start_upi(client: httpx.AsyncClient, vpa: str = "kiran@okbank") -> str:
    res = await client.post("/v1/payments", json=pay("250.00", f"upi:{vpa}"), headers=with_key(A))
    assert res.status_code == 202
    assert res.json()["payment_method"]["type"] == "upi"
    return str(res.json()["id"])


async def bank_webhook(bank: BankState, payment_id: str) -> SentWebhook:
    """Waits for SimBank to settle the payment and produce its webhook."""
    for _ in range(100):
        for sent in bank.webhooks:
            if json.loads(sent.body)["charge"]["reference"] == payment_id:
                return sent
        await asyncio.sleep(0.02)
    raise AssertionError("SimBank sent no webhook")


async def deliver(client: httpx.AsyncClient, sent: SentWebhook) -> httpx.Response:
    return await client.post("/webhooks/simbank", content=sent.body, headers=sent.headers)


async def status_of(client: httpx.AsyncClient, payment_id: str) -> str:
    return str((await client.get(f"/v1/payments/{payment_id}", headers=A)).json()["status"])


async def count(model: type) -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_upi_payment_succeeds_when_the_banks_webhook_arrives(
    client: httpx.AsyncClient, bank: BankState
) -> None:
    payment_id = await start_upi(client)
    assert await status_of(client, payment_id) == "processing"

    res = await deliver(client, await bank_webhook(bank, payment_id))

    assert res.status_code == 200
    assert res.json() == {"status": "processed"}
    assert await status_of(client, payment_id) == "succeeded"
    balance = await client.get("/v1/balance", headers=A)
    assert balance.json()["available"] == "245.00"
    async with SessionLocal() as session:
        assert await session.scalar(select(Event.type)) == "payment.succeeded"


async def test_a_resent_webhook_is_a_harmless_duplicate(
    client: httpx.AsyncClient, bank: BankState
) -> None:
    sent = await bank_webhook(bank, await start_upi(client))

    first, again = await deliver(client, sent), await deliver(client, sent)

    assert first.json()["status"] == "processed"
    assert again.status_code == 200  # 2xx, so the bank stops resending
    assert again.json()["status"] == "duplicate"
    assert await count(Event) == 1
    assert await count(InboundWebhook) == 1


async def test_two_copies_at_the_same_moment_are_processed_once(
    client: httpx.AsyncClient, bank: BankState
) -> None:
    sent = await bank_webhook(bank, await start_upi(client))

    results = await asyncio.gather(*(deliver(client, sent) for _ in range(5)))

    assert sorted(r.json()["status"] for r in results) == ["duplicate"] * 4 + ["processed"]
    assert await count(Event) == 1


async def test_declined_upi_payment_fails(client: httpx.AsyncClient, bank: BankState) -> None:
    payment_id = await start_upi(client, "fail@okbank")
    await deliver(client, await bank_webhook(bank, payment_id))

    payment = (await client.get(f"/v1/payments/{payment_id}", headers=A)).json()
    assert (payment["status"], payment["failure_code"]) == ("failed", "upi_declined")


@pytest.mark.parametrize("problem", ["no signature", "wrong secret", "changed body", "too old"])
async def test_unsigned_or_forged_webhooks_are_401(
    client: httpx.AsyncClient, bank: BankState, problem: str
) -> None:
    payment_id = await start_upi(client)
    sent = await bank_webhook(bank, payment_id)
    body, headers = sent.body, dict(sent.headers)
    if problem == "no signature":
        del headers["SimBank-Signature"]
    elif problem == "wrong secret":
        headers["SimBank-Signature"] = sign("whsec_guess", body)
    elif problem == "changed body":
        body = body.replace(b'"approved"', b'"declined"')
    else:
        headers["SimBank-Signature"] = sign(SECRET, body, int(time.time()) - 3600)

    res = await client.post("/webhooks/simbank", content=body, headers=headers)

    assert res.status_code == 401
    assert res.json()["error"]["code"] == "invalid_signature"
    assert await status_of(client, payment_id) == "processing"


async def test_signed_but_unknown_payload_is_400(client: httpx.AsyncClient) -> None:
    body = b'{"id": "x", "type": "something.else"}'
    res = await client.post(
        "/webhooks/simbank", content=body, headers={"SimBank-Signature": sign(SECRET, body)}
    )
    assert res.status_code == 400


async def test_webhook_for_a_payment_we_dont_know_is_acknowledged_and_ignored(
    client: httpx.AsyncClient,
) -> None:
    body = json.dumps(
        {
            "id": "sbevt_1",
            "type": "charge.updated",
            "charge": {
                "charge_id": "ch_1",
                "reference": "pay_nobody",
                "status": "approved",
                "decline_code": None,
            },
        }
    ).encode()
    res = await client.post(
        "/webhooks/simbank", content=body, headers={"SimBank-Signature": sign(SECRET, body)}
    )
    assert res.json()["status"] == "ignored"


async def test_reconciliation_finishes_upi_if_the_webhook_never_comes(
    client: httpx.AsyncClient, bank: BankState
) -> None:
    payment_id = await start_upi(client)
    await bank_webhook(bank, payment_id)  # settled at the bank, but we never got told
    async with SessionLocal.begin() as session:
        await session.execute(
            update(Payment).values(created_at=utcnow() - STUCK_AFTER - STUCK_AFTER)
        )

    assert await reconcile.reconcile_payments() == 1
    assert await status_of(client, payment_id) == "succeeded"
