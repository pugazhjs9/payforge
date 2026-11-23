"""Reconciliation: finishing payments and refunds whose bank answer we never got.

A bank timeout leaves a payment `processing` and a refund `pending` (ADR 0003). These
jobs ask the bank, by our reference, what really happened, and finalize accordingly.
They are safe to run on several workers at once: finalizing is compare-and-set, so a
payment can only be finished once, whoever gets there first (this job, a bank webhook,
or another worker).
"""

import asyncio
from datetime import timedelta

from sqlalchemy import delete, select

from payforge.clients import simbank
from payforge.db import SessionLocal
from payforge.logging import log
from payforge.models import IdempotencyKey, Payment, PaymentStatus, Refund, RefundStatus, utcnow
from payforge.services import payments, refunds

STUCK_AFTER = timedelta(seconds=30)  # much longer than the bank timeout (3 s)
BATCH = 50
PARALLEL_LOOKUPS = 10


async def reconcile_payments() -> int:
    """Returns how many stuck payments were finished."""
    async with SessionLocal() as session:
        stuck = list(
            await session.scalars(
                select(Payment.id)
                .where(
                    Payment.status == PaymentStatus.PROCESSING,
                    Payment.created_at < utcnow() - STUCK_AFTER,
                )
                .order_by(Payment.created_at)
                .limit(BATCH)
            )
        )
    limit = asyncio.Semaphore(PARALLEL_LOOKUPS)

    async def check(payment_id: str) -> bool:
        async with limit:
            outcome = await simbank.get_charge(payment_id)
        match outcome:
            case simbank.Approved(charge_id=charge_id):
                result: payments.PaymentResult = payments.PaymentSucceeded(charge_id)
            case simbank.Declined(charge_id=charge_id, decline_code=code):
                result = payments.PaymentFailed(failure_code=code, bank_charge_id=charge_id)
            case simbank.NotFoundAtBank():
                # Our request never reached the bank. It can't arrive this late (our
                # client gave up after 3 s), and the bank dedupes by reference anyway.
                result = payments.PaymentFailed(failure_code="not_received_by_bank")
            case _:
                return False  # still pending at the bank (UPI), or the bank is down
        finished = await payments.finalize_payment(payment_id, result)
        if finished:
            log.info("payment_reconciled", payment_id=payment_id, outcome=type(outcome).__name__)
        return finished

    return sum(await asyncio.gather(*(check(p) for p in stuck)))


async def reconcile_refunds() -> int:
    """Returns how many stuck refunds were finished."""
    async with SessionLocal() as session:
        stuck = list(
            await session.scalars(
                select(Refund)
                .where(
                    Refund.status == RefundStatus.PENDING,
                    Refund.created_at < utcnow() - STUCK_AFTER,
                )
                .order_by(Refund.created_at)
                .limit(BATCH)
            )
        )
    limit = asyncio.Semaphore(PARALLEL_LOOKUPS)

    async def check(refund: Refund) -> bool:
        async with limit:
            outcome = await simbank.get_refund(refund.id)
            if isinstance(outcome, simbank.NotFoundAtBank):
                # never reached the bank: send it again. The merchant asked for this
                # refund, and the bank dedupes by our reference.
                _, final = await refunds.process_refund(refund)
                return final
        match outcome:
            case simbank.RefundSucceeded():
                return await refunds.finalize_refund(refund.id, refunds.RefundSucceededResult())
            case simbank.RefundFailed(failure_code=code):
                return await refunds.finalize_refund(
                    refund.id, refunds.RefundFailedResult(failure_code=code)
                )
            case _:
                return False

    return sum(await asyncio.gather(*(check(r) for r in stuck)))


async def delete_expired_idempotency_keys(batch: int = 1000) -> int:
    """Deletes in small batches, so no single statement locks many rows for long."""
    total = 0
    while True:
        async with SessionLocal.begin() as session:
            result = await session.execute(
                delete(IdempotencyKey)
                .where(IdempotencyKey.expires_at < utcnow())
                .with_dialect_options(mysql_limit=batch)
            )
        deleted = int(getattr(result, "rowcount", 0))
        total += deleted
        if deleted < batch:
            return total
