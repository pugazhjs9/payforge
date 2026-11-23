"""Refunds: reserve atomically, ask the bank, finalize exactly once."""

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from payforge.clients import simbank
from payforge.db import SessionLocal
from payforge.domain.money import to_minor
from payforge.errors import Conflict, NotFound, ValidationFailed
from payforge.ids import new_id
from payforge.models import Payment, PaymentStatus, Refund, RefundStatus, utcnow
from payforge.services.ledger import forget_balance, post_transaction, refund_transfers
from payforge.services.payments import get_payment


@dataclass(frozen=True)
class NewRefund:
    amount: str | None = None  # None = everything that is left
    reason: str | None = None


@dataclass(frozen=True)
class RefundSucceededResult:
    pass


@dataclass(frozen=True)
class RefundFailedResult:
    failure_code: str


RefundResult = RefundSucceededResult | RefundFailedResult


async def reserve_refund(
    merchant_id: str, payment_id: str, data: NewRefund, refund_id: str | None = None
) -> Refund:
    """Step 1, in one database transaction:

    - reserve the amount on the payment with ONE conditional UPDATE, so two refunds
      racing each other can never take more than the payment amount;
    - insert the refund row as `pending`.
    The bank is not called yet.
    """
    payment = await get_payment(merchant_id, payment_id)  # 404 for other merchants
    if data.amount is not None:
        amount_minor = to_minor(data.amount)
        if amount_minor <= 0:
            raise ValidationFailed("amount", "must be greater than 0.00")
    else:
        amount_minor = payment.amount_minor - payment.amount_refunded_minor
        if amount_minor <= 0:
            raise Conflict("nothing_to_refund", "This payment has already been fully refunded.")

    async with SessionLocal.begin() as session:
        # The check and the write are one statement: InnoDB applies racing updates to
        # the same row one after another, re-checking the WHERE clause each time.
        reserved = cast(
            CursorResult[Any],
            await session.execute(
                update(Payment)
                .where(
                    Payment.id == payment_id,
                    Payment.merchant_id == merchant_id,
                    Payment.status == PaymentStatus.SUCCEEDED,
                    Payment.amount_refunded_minor + amount_minor <= Payment.amount_minor,
                )
                .values(
                    amount_refunded_minor=Payment.amount_refunded_minor + amount_minor,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if reserved.rowcount == 0:
            status = await session.scalar(select(Payment.status).where(Payment.id == payment_id))
            if status != PaymentStatus.SUCCEEDED:
                raise Conflict(
                    "payment_not_refundable",
                    f"Only succeeded payments can be refunded (this one is {status}).",
                )
            raise Conflict(
                "refund_exceeds_remaining", "The refund is larger than the amount left to refund."
            )

        refund = Refund(
            id=refund_id or new_id("re"),
            payment_id=payment_id,
            merchant_id=merchant_id,
            amount_minor=amount_minor,
            reason=data.reason,
        )
        session.add(refund)
    return refund


async def load_refund(refund_id: str) -> Refund:
    async with SessionLocal() as session:
        refund = await session.get(Refund, refund_id)
    if refund is None:
        raise NotFound("Refund")
    return refund


async def process_refund(refund: Refund) -> tuple[Refund, bool]:
    """Step 2: ask the bank (reference = refund ID) and record the answer."""
    outcome = await simbank.refund(
        reference=refund.id, charge_reference=refund.payment_id, amount_minor=refund.amount_minor
    )
    match outcome:
        case simbank.RefundSucceeded():
            await finalize_refund(refund.id, RefundSucceededResult())
        case simbank.RefundFailed(failure_code=code):
            await finalize_refund(refund.id, RefundFailedResult(failure_code=code))
        case _:
            pass  # Unknown: stays pending; resolved later
    latest = await load_refund(refund.id)
    return latest, latest.status != RefundStatus.PENDING


async def create_refund(merchant_id: str, payment_id: str, data: NewRefund) -> tuple[Refund, bool]:
    return await process_refund(await reserve_refund(merchant_id, payment_id, data))


async def finalize_refund(refund_id: str, result: RefundResult) -> bool:
    """Moves a refund out of `pending` exactly once (compare-and-set).
    Succeeded -> ledger transfers. Failed -> the reserved amount is given back."""
    failed = isinstance(result, RefundFailedResult)
    async with SessionLocal.begin() as session:
        changed = cast(
            CursorResult[Any],
            await session.execute(
                update(Refund)
                .where(Refund.id == refund_id, Refund.status == RefundStatus.PENDING)
                .values(
                    status=RefundStatus.FAILED if failed else RefundStatus.SUCCEEDED,
                    failure_code=result.failure_code
                    if isinstance(result, RefundFailedResult)
                    else None,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if changed.rowcount == 0:
            return False

        refund = await session.get_one(Refund, refund_id)
        if failed:
            await session.execute(
                update(Payment)
                .where(Payment.id == refund.payment_id)
                .values(
                    amount_refunded_minor=Payment.amount_refunded_minor - refund.amount_minor,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            )
        else:
            await post_transaction(
                session,
                key=f"refund:{refund_id}",
                merchant_id=refund.merchant_id,
                transfers=refund_transfers(refund.merchant_id, refund.amount_minor),
            )
    if not failed:
        await forget_balance(refund.merchant_id)  # after COMMIT
    return True


async def list_refunds(merchant_id: str, payment_id: str) -> list[Refund]:
    await get_payment(merchant_id, payment_id)  # 404 if not theirs
    async with SessionLocal() as session:
        return list(
            await session.scalars(
                select(Refund)
                .where(Refund.payment_id == payment_id, Refund.merchant_id == merchant_id)
                .order_by(Refund.created_at, Refund.id)
            )
        )
