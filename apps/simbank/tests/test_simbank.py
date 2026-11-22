import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from simbank.app import BankState, create_app


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
