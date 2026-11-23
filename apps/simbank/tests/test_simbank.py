import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from simbank.app import BankState, create_app, sign


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bank")


@pytest.fixture
async def bank() -> AsyncIterator[tuple[httpx.AsyncClient, BankState]]:
    app = create_app(slow_seconds=0.3)
    async with make_client(app) as client:
        yield client, app.state.bank


def charge(reference: str, token: str, amount_minor: int = 10_000) -> dict[str, object]:
    return {"reference": reference, "amount_minor": amount_minor, "currency": "INR", "token": token}


@pytest.mark.parametrize(
    ("token", "status", "decline_code"),
    [
        ("tok_card_ok", "approved", None),
        ("tok_card_declined", "declined", "card_declined"),
        ("tok_card_no_funds", "declined", "insufficient_funds"),
        ("tok_card_unknown", "declined", "invalid_token"),
    ],
)
async def test_card_tokens(
    bank: tuple[httpx.AsyncClient, BankState], token: str, status: str, decline_code: str | None
) -> None:
    client, _ = bank
    res = await client.post("/charges", json=charge("pay_1", token))

    assert res.status_code == 200
    assert res.json()["status"] == status
    assert res.json()["decline_code"] == decline_code


async def test_same_reference_twice_charges_once(bank: tuple[httpx.AsyncClient, BankState]) -> None:
    client, state = bank
    first = await client.post("/charges", json=charge("pay_1", "tok_card_ok"))
    second = await client.post("/charges", json=charge("pay_1", "tok_card_ok"))

    assert second.json()["charge_id"] == first.json()["charge_id"]
    assert len(state.charges) == 1


async def test_slow_charge_is_recorded_before_the_answer(
    bank: tuple[httpx.AsyncClient, BankState],
) -> None:
    client, _ = bank
    pending = asyncio.create_task(client.post("/charges", json=charge("pay_slow", "tok_card_slow")))
    await asyncio.sleep(0.05)

    lookup = await client.get("/charges/pay_slow")
    assert lookup.json()["status"] == "approved"
    assert (await pending).json()["status"] == "approved"


async def test_unknown_reference_is_404(bank: tuple[httpx.AsyncClient, BankState]) -> None:
    client, _ = bank
    assert (await client.get("/charges/nope")).status_code == 404


async def test_refund_is_idempotent_by_reference(bank: tuple[httpx.AsyncClient, BankState]) -> None:
    client, _ = bank
    await client.post("/charges", json=charge("pay_1", "tok_card_ok"))
    body = {"reference": "re_1", "charge_reference": "pay_1", "amount_minor": 4_000}

    a = await client.post("/refunds", json=body)
    b = await client.post("/refunds", json=body)

    assert a.json()["status"] == "succeeded"
    assert b.json()["refund_id"] == a.json()["refund_id"]


@pytest.mark.parametrize(
    ("token", "amount", "code"),
    [
        ("tok_card_ok", 20_000, "amount_too_large"),
        ("tok_card_declined", 1_000, "charge_not_refundable"),
        ("tok_card_no_refunds", 1_000, "refund_not_allowed"),
    ],
)
async def test_refund_failures(
    bank: tuple[httpx.AsyncClient, BankState], token: str, amount: int, code: str
) -> None:
    client, _ = bank
    await client.post("/charges", json=charge("pay_1", token))
    res = await client.post(
        "/refunds", json={"reference": "re_1", "charge_reference": "pay_1", "amount_minor": amount}
    )

    assert res.json()["status"] == "failed"
    assert res.json()["failure_code"] == code


# ── UPI: asynchronous, settled later, announced by a signed webhook ───────────


async def settled(bank: BankState, count: int = 1) -> None:
    for _ in range(100):
        if len(bank.webhooks) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no webhook")


def upi_bank(**options: object) -> FastAPI:
    return create_app(upi_seconds=0.05, webhook_secret="whsec_test", **options)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("vpa", "status", "code"),
    [
        ("kiran@okbank", "approved", None),
        ("fail@okbank", "declined", "upi_declined"),
        ("expire@okbank", "declined", "upi_expired"),
    ],
)
async def test_upi_is_pending_then_settled_with_a_signed_webhook(
    vpa: str, status: str, code: str | None
) -> None:
    app = upi_bank()
    state: BankState = app.state.bank
    async with make_client(app) as client:
        res = await client.post("/charges", json=charge("pay_upi", f"upi:{vpa}"))
        assert res.json()["status"] == "pending"
        await settled(state)

    [sent] = state.webhooks
    payload = json.loads(sent.body)
    assert payload["type"] == "charge.updated"
    assert (payload["charge"]["status"], payload["charge"]["decline_code"]) == (status, code)
    timestamp = sent.headers["SimBank-Signature"].split(",")[0].removeprefix("t=")
    assert sent.headers["SimBank-Signature"] == sign("whsec_test", sent.body, int(timestamp))


async def test_upi_webhook_is_retried_until_it_gets_a_2xx() -> None:
    answers = iter([500, 503, 200])
    calls: list[httpx.Request] = []

    def receiver(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(next(answers))

    app = upi_bank(
        webhook_url="http://payforge/webhooks/simbank",
        webhook_transport=httpx.MockTransport(receiver),
        retry_delays=(0.01, 0.01, 0.01),
    )
    async with make_client(app) as client:
        await client.post("/charges", json=charge("pay_upi", "upi:kiran@okbank"))
        await settled(app.state.bank)

    assert len(calls) == 3
    assert app.state.bank.webhooks[0].delivered is True


async def test_same_upi_reference_twice_is_one_charge_and_one_webhook() -> None:
    app = upi_bank()
    async with make_client(app) as client:
        first = await client.post("/charges", json=charge("pay_upi", "upi:kiran@okbank"))
        again = await client.post("/charges", json=charge("pay_upi", "upi:kiran@okbank"))
        await settled(app.state.bank)
        await asyncio.sleep(0.1)

    assert first.json()["charge_id"] == again.json()["charge_id"]
    assert len(app.state.bank.webhooks) == 1
