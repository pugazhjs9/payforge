"""The idempotency key service, against real MySQL (row locks, unique keys)."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from payforge.db import SessionLocal
from payforge.errors import AppError
from payforge.models import IdempotencyKey, IdempotencyStatus, utcnow
from payforge.services import idempotency
from payforge.services.idempotency import Claim, Replay

from .conftest import MERCHANT_A, MERCHANT_B

HASH = idempotency.fingerprint("POST", "/v1/payments", {"amount": "10.00"})
OTHER_HASH = idempotency.fingerprint("POST", "/v1/payments", {"amount": "99.00"})


async def claim(key: str = "k1", request_hash: str = HASH, merchant_id: str = MERCHANT_A) -> Claim:
    result = await idempotency.claim(merchant_id, key, request_hash)
    assert isinstance(result, Claim)
    return result


async def load(key: str = "k1") -> IdempotencyKey | None:
    async with SessionLocal() as session:
        return await session.get(IdempotencyKey, (MERCHANT_A, key))


async def set_columns(key: str = "k1", **values: object) -> None:
    async with SessionLocal.begin() as session:
        await session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.merchant_id == MERCHANT_A, IdempotencyKey.key == key)
            .values(**values)
        )


async def link(c: Claim, resource_id: str) -> None:
    async with SessionLocal.begin() as session:
        await idempotency.link(session, c, resource_id)


def test_fingerprint_ignores_key_order_but_not_values() -> None:
    a = idempotency.fingerprint("POST", "/v1/payments", {"amount": "1.00", "currency": "INR"})
    b = idempotency.fingerprint("POST", "/v1/payments", {"currency": "INR", "amount": "1.00"})
    assert a == b
    assert a != idempotency.fingerprint("POST", "/v1/payments", {"amount": "2.00"})
    assert a != idempotency.fingerprint("POST", "/v1/refunds", {"amount": "1.00"})


async def test_second_claim_while_running_is_409_with_retry_after() -> None:
    await claim()

    with pytest.raises(AppError) as error:
        await idempotency.claim(MERCHANT_A, "k1", HASH)
    assert error.value.status == 409
    assert error.value.code == "idempotency_key_in_use"
    assert error.value.headers == {"Retry-After": "1"}


async def test_same_key_for_a_different_request_is_422() -> None:
    await claim()

    with pytest.raises(AppError) as error:
        await idempotency.claim(MERCHANT_A, "k1", OTHER_HASH)
    assert (error.value.status, error.value.code) == (422, "idempotency_key_reused")


async def test_completed_key_replays_the_saved_response() -> None:
    first = await claim()
    await link(first, "pay_1")
    await idempotency.complete(first, 201, {"id": "pay_1", "status": "succeeded"})

    assert await idempotency.claim(MERCHANT_A, "k1", HASH) == Replay(
        201, {"id": "pay_1", "status": "succeeded"}
    )


async def test_fifty_parallel_claims_exactly_one_wins() -> None:
    results = await asyncio.gather(
        *(idempotency.claim(MERCHANT_A, "k1", HASH) for _ in range(50)), return_exceptions=True
    )

    assert sum(isinstance(r, Claim) for r in results) == 1
    losers = [r for r in results if not isinstance(r, Claim)]
    assert all(isinstance(r, AppError) and r.status == 409 for r in losers)


async def test_stale_lock_is_taken_over_and_resumes_the_saved_resource() -> None:
    crashed = await claim()
    await link(crashed, "pay_1")
    await set_columns(locked_at=utcnow() - timedelta(minutes=2))  # worker died long ago

    resumed = await claim()

    assert resumed.resource_id == "pay_1"  # continue with the same payment, don't make a 2nd
    assert resumed.lock_token != crashed.lock_token


async def test_the_old_owner_is_fenced_off_after_a_takeover() -> None:
    old = await claim()
    await set_columns(locked_at=utcnow() - timedelta(minutes=2))
    new = await claim()

    with pytest.raises(AppError):  # the old owner can no longer attach its resource
        await link(old, "pay_old")
    await idempotency.complete(old, 201, {"id": "pay_old"})  # ignored
    row = await load()
    assert row is not None
    assert row.status == IdempotencyStatus.IN_PROGRESS
    assert row.lock_token == new.lock_token


async def test_release_before_anything_was_created_forgets_the_key() -> None:
    first = await claim()
    await idempotency.release(first)

    assert await load() is None
    await claim(request_hash=OTHER_HASH)  # free to use again, even for another request


async def test_release_after_creating_unlocks_for_an_immediate_retry() -> None:
    first = await claim()
    await link(first, "pay_1")
    await idempotency.release(first)

    retry = await claim()  # no 60 s wait
    assert retry.resource_id == "pay_1"


async def test_expired_key_starts_over() -> None:
    first = await claim()
    await idempotency.complete(first, 201, {"id": "pay_1"})
    await set_columns(expires_at=utcnow() - timedelta(seconds=1))

    again = await claim(request_hash=OTHER_HASH)
    assert again.resource_id is None


async def test_keys_are_case_sensitive_and_per_merchant() -> None:
    await claim("Order-1")
    await claim("order-1")
    await claim("Order-1", merchant_id=MERCHANT_B)
