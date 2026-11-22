import asyncio
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from payforge.config import settings
from payforge.db import SessionLocal
from payforge.models import LedgerTransaction, LedgerTransfer, Payment, PaymentStatus
from payforge.services.payments import PaymentFailed, PaymentSucceeded, finalize_payment
from simbank.app import BankState

from .conftest import MERCHANT_A, MERCHANT_B, merchant, pay

A = merchant(MERCHANT_A)
B = merchant(MERCHANT_B)
pytestmark = pytest.mark.usefixtures("bank")


async def count(model: type) -> int:
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ── POST /v1/payments ─────────────────────────────────────────────────────────


async def test_approved_card_is_201_succeeded_and_credits_the_balance(
    client: httpx.AsyncClient,
) -> None:
    res = await client.post(
        "/v1/payments", json=pay("499.00", description="Order #1042"), headers=A
    )

    assert res.status_code == 201
    body = res.json()
    assert re.fullmatch(r"pay_[0-9A-Z]{26}", body["id"])
    assert body["status"] == "succeeded"
    assert body["amount"] == "499.00"
    assert body["fee"] == "9.98"
    assert body["payment_method"] == {"type": "card", "token": "tok_card_ok"}
    assert body["description"] == "Order #1042"

    balance = await client.get("/v1/balance", headers=A)
    assert balance.json() == {"object": "balance", "currency": "INR", "available": "489.02"}


@pytest.mark.parametrize(
    ("token", "code"),
    [("tok_card_declined", "card_declined"), ("tok_card_no_funds", "insufficient_funds")],
)
async def test_decline_is_still_201_with_status_failed(
    client: httpx.AsyncClient, token: str, code: str
) -> None:
    res = await client.post("/v1/payments", json=pay("100.00", token), headers=A)

    assert res.status_code == 201
    assert res.json()["status"] == "failed"
    assert res.json()["failure_code"] == code
    assert res.json()["fee"] == "0.00"
    assert await count(LedgerTransfer) == 0


async def test_bank_slower_than_our_timeout_is_202_processing(
    client: httpx.AsyncClient, bank: BankState
) -> None:
    res = await client.post("/v1/payments", json=pay("100.00", "tok_card_slow"), headers=A)

    assert res.status_code == 202
    assert res.json()["status"] == "processing"
    # the bank DID record the charge: that is why "timeout" must never mean "failed"
    assert bank.charges[res.json()["id"]].status == "approved"


async def test_bank_unreachable_is_202_not_500(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "simbank_url", "http://127.0.0.1:1")
    res = await client.post("/v1/payments", json=pay("100.00"), headers=A)

    assert res.status_code == 202
    assert res.json()["status"] == "processing"


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"amount": 499}, "amount"),
        ({"amount": "1.234"}, "amount"),
        ({"amount": "0.50"}, "amount"),
        ({"amount": "500000.01"}, "amount"),
        ({"currency": "USD"}, "currency"),
        ({"payment_method": "cash"}, "payment_method"),
        ({"customer_email": "not-an-email"}, "customer_email"),
        ({"ammount": "1.00"}, "ammount"),
    ],
)
async def test_invalid_values_are_422_with_details(
    client: httpx.AsyncClient, override: dict[str, object], field: str
) -> None:
    res = await client.post("/v1/payments", json={**pay("100.00"), **override}, headers=A)

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_failed"
    assert field in [d["field"] for d in res.json()["error"]["details"]]


async def test_broken_json_is_400(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/v1/payments", content=b'{"amount": ', headers={**A, "content-type": "application/json"}
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "invalid_json"


async def test_non_json_body_is_415(client: httpx.AsyncClient) -> None:
    res = await client.post(
        "/v1/payments", content=b"amount=100", headers={**A, "content-type": "text/plain"}
    )
    assert res.status_code == 415


async def test_no_access_token_is_401(client: httpx.AsyncClient) -> None:
    res = await client.post("/v1/payments", json=pay("1.00"))
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "missing_token"


async def test_the_old_x_merchant_id_header_no_longer_works(client: httpx.AsyncClient) -> None:
    res = await client.post("/v1/payments", json=pay("1.00"), headers={"X-Merchant-Id": MERCHANT_A})
    assert res.status_code == 401


# ── GET /v1/payments/{id} ─────────────────────────────────────────────────────


async def test_get_own_payment(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/payments", json=pay("10.00"), headers=A)
    res = await client.get(f"/v1/payments/{created.json()['id']}", headers=A)

    assert res.status_code == 200
    assert res.json()["id"] == created.json()["id"]


async def test_another_merchants_payment_is_404_not_403(client: httpx.AsyncClient) -> None:
    created = await client.post("/v1/payments", json=pay("10.00"), headers=A)
    res = await client.get(f"/v1/payments/{created.json()['id']}", headers=B)

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


# ── GET /v1/payments (cursor pagination) ──────────────────────────────────────


async def seed_payments(n: int, merchant_id: str = MERCHANT_A) -> None:
    """Five rows share each timestamp on purpose: the id must break ties."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with SessionLocal.begin() as session:
        for i in range(n):
            session.add(
                Payment(
                    id=f"pay_{merchant_id}_{i:04d}",
                    merchant_id=merchant_id,
                    amount_minor=1_000 + i,
                    currency="INR",
                    status=PaymentStatus.FAILED if i % 3 == 0 else PaymentStatus.SUCCEEDED,
                    method="card",
                    method_token="tok_card_ok",
                    created_at=now - timedelta(seconds=i // 5),
                )
            )


async def test_walks_45_payments_as_20_20_5_newest_first_without_duplicates(
    client: httpx.AsyncClient,
) -> None:
    await seed_payments(45)
    await seed_payments(3, MERCHANT_B)

    seen: list[dict[str, str]] = []
    sizes: list[int] = []
    cursor: str | None = None
    while True:
        params = {"limit": 20, **({"cursor": cursor} if cursor else {})}
        res = await client.get("/v1/payments", params=params, headers=A)
        assert res.status_code == 200
        sizes.append(len(res.json()["data"]))
        seen.extend(res.json()["data"])
        cursor = res.json()["next_cursor"]
        assert res.json()["has_more"] is (cursor is not None)
        if cursor is None:
            break

    assert sizes == [20, 20, 5]
    assert len({p["id"] for p in seen}) == 45
    keys = [(p["created_at"], p["id"]) for p in seen]
    assert keys == sorted(keys, reverse=True)


async def test_new_payments_between_pages_cause_no_duplicates(client: httpx.AsyncClient) -> None:
    await seed_payments(30)
    first = await client.get("/v1/payments", params={"limit": 10}, headers=A)
    await client.post("/v1/payments", json=pay("5.00"), headers=A)
    await client.post("/v1/payments", json=pay("6.00"), headers=A)
    second = await client.get(
        "/v1/payments", params={"limit": 10, "cursor": first.json()["next_cursor"]}, headers=A
    )

    first_ids = {p["id"] for p in first.json()["data"]}
    assert not first_ids & {p["id"] for p in second.json()["data"]}


async def test_status_filter(client: httpx.AsyncClient) -> None:
    await seed_payments(30)
    res = await client.get("/v1/payments", params={"status": "failed", "limit": 100}, headers=A)

    assert len(res.json()["data"]) == 10
    assert {p["status"] for p in res.json()["data"]} == {"failed"}


async def test_tampered_cursor_is_400_and_limit_over_100_is_422(client: httpx.AsyncClient) -> None:
    bad = await client.get("/v1/payments", params={"cursor": "not-a-cursor"}, headers=A)
    assert bad.json()["error"]["code"] == "invalid_cursor"
    assert (await client.get("/v1/payments", params={"limit": 101}, headers=A)).status_code == 422


# ── finalizing ────────────────────────────────────────────────────────────────


async def test_racing_finalizers_exactly_one_wins_and_ledger_written_once(
    client: httpx.AsyncClient,
) -> None:
    res = await client.post("/v1/payments", json=pay("100.00", "tok_card_slow"), headers=A)
    payment_id = res.json()["id"]

    results = await asyncio.gather(
        finalize_payment(payment_id, PaymentSucceeded()),
        finalize_payment(payment_id, PaymentSucceeded()),
        finalize_payment(payment_id, PaymentFailed(failure_code="expired")),
    )

    assert results.count(True) == 1
    async with SessionLocal() as session:
        payment = await session.get_one(Payment, payment_id)
        postings = await session.scalar(
            select(func.count())
            .select_from(LedgerTransaction)
            .where(LedgerTransaction.transaction_key == f"payment:{payment_id}")
        )
    assert postings == (1 if payment.status == PaymentStatus.SUCCEEDED else 0)
