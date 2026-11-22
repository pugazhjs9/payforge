"""Payments: create, charge through the bank, finalize exactly once, read and list."""

import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from payforge.clients import simbank
from payforge.db import SessionLocal
from payforge.domain.cursor import Cursor, encode_cursor
from payforge.domain.money import MAX_AMOUNT_MINOR, MIN_AMOUNT_MINOR, fee_for, to_minor
from payforge.errors import NotFound, ValidationFailed
from payforge.ids import new_id
from payforge.models import Merchant, Payment, PaymentStatus, utcnow
from payforge.services.ledger import payment_transfers, post_transaction

CARD_TOKEN = re.compile(r"tok_card_[a-z_]+", re.ASCII)


@dataclass(frozen=True)
class MerchantRef:
    id: str


@dataclass(frozen=True)
class NewPayment:
    amount: str
    currency: Literal["INR"]
    payment_method: str
    description: str | None = None
    customer_email: str | None = None
    metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class PaymentSucceeded:
    bank_charge_id: str | None = None


@dataclass(frozen=True)
class PaymentFailed:
    failure_code: str
    bank_charge_id: str | None = None


PaymentResult = PaymentSucceeded | PaymentFailed


@dataclass(frozen=True)
class Page:
    data: list[Payment]
    has_more: bool
    next_cursor: str | None


def parse_amount(amount: str, field: str = "amount") -> int:
    """Amount string -> paise, within PayForge's limits (₹1.00 to ₹5,00,000)."""
    minor = to_minor(amount)
    if not MIN_AMOUNT_MINOR <= minor <= MAX_AMOUNT_MINOR:
        raise ValidationFailed(field, "must be between 1.00 and 500000.00")
    return minor


def payment_method_of(token: str) -> str:
    if CARD_TOKEN.fullmatch(token):
        return "card"
    raise ValidationFailed("payment_method", "unknown payment method")


async def insert_payment(
    session: AsyncSession, merchant: MerchantRef, data: NewPayment, payment_id: str | None = None
) -> Payment:
    """Creates the payment row in `processing`. The bank is not called yet."""
    payment = Payment(
        id=payment_id or new_id("pay"),
        merchant_id=merchant.id,
        amount_minor=parse_amount(data.amount),
        currency=data.currency,
        method=payment_method_of(data.payment_method),
        method_token=data.payment_method,
        description=data.description,
        customer_email=data.customer_email,
        metadata_=data.metadata or {},
    )
    session.add(payment)
    await session.flush()
    return payment


async def load_payment(payment_id: str) -> Payment:
    async with SessionLocal() as session:
        payment = await session.get(Payment, payment_id)
    if payment is None:
        raise NotFound("Payment")
    return payment


async def process_payment(payment: Payment) -> tuple[Payment, bool]:
    """Asks the bank to charge, using the payment ID as the bank reference, and records
    the answer. Returns (payment, final); final is False when the outcome isn't known yet."""
    outcome = await simbank.charge(
        reference=payment.id,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        token=payment.method_token,
    )
    match outcome:
        case simbank.Approved(charge_id=charge_id):
            await finalize_payment(payment.id, PaymentSucceeded(bank_charge_id=charge_id))
        case simbank.Declined(charge_id=charge_id, decline_code=code):
            await finalize_payment(
                payment.id, PaymentFailed(failure_code=code, bank_charge_id=charge_id)
            )
        case _:
            pass  # Pending / Unknown: stays processing; resolved later
    latest = await load_payment(payment.id)
    return latest, latest.status != PaymentStatus.PROCESSING


async def create_payment(merchant: MerchantRef, data: NewPayment) -> tuple[Payment, bool]:
    async with SessionLocal.begin() as session:
        payment = await insert_payment(session, merchant, data)
    return await process_payment(payment)


async def finalize_payment(payment_id: str, result: PaymentResult) -> bool:
    """Moves a payment out of `processing` exactly once.

    The status check is part of the UPDATE (compare-and-set), so if two processes race
    (bank webhook vs. checker job), only one wins and only one ledger transaction is
    written. Returns True if this call made the change.
    """
    async with SessionLocal.begin() as session:
        row = (
            await session.execute(
                select(
                    Payment.merchant_id,
                    Payment.amount_minor,
                    Payment.bank_charge_id,
                    Merchant.fee_bps,
                )
                .join(Merchant, Merchant.id == Payment.merchant_id)
                .where(Payment.id == payment_id)
            )
        ).one()
        succeeded = isinstance(result, PaymentSucceeded)
        fee_minor = fee_for(row.amount_minor, row.fee_bps) if succeeded else 0

        changed = cast(
            CursorResult[Any],
            await session.execute(
                update(Payment)
                .where(Payment.id == payment_id, Payment.status == PaymentStatus.PROCESSING)
                .values(
                    status=PaymentStatus.SUCCEEDED if succeeded else PaymentStatus.FAILED,
                    failure_code=result.failure_code if isinstance(result, PaymentFailed) else None,
                    bank_charge_id=result.bank_charge_id or row.bank_charge_id,
                    fee_minor=fee_minor,
                    updated_at=utcnow(),
                )
                .execution_options(synchronize_session=False)
            ),
        )
        if changed.rowcount == 0:
            return False  # someone else already finished it

        if succeeded:
            await post_transaction(
                session,
                key=f"payment:{payment_id}",
                merchant_id=row.merchant_id,
                transfers=payment_transfers(row.merchant_id, row.amount_minor, fee_minor),
            )
        return True


async def get_payment(merchant_id: str, payment_id: str) -> Payment:
    """merchant_id is part of the query: another merchant's payment is simply 'not found'."""
    async with SessionLocal() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.id == payment_id, Payment.merchant_id == merchant_id)
        )
    if payment is None:
        raise NotFound("Payment")
    return payment


async def list_payments(
    merchant_id: str,
    *,
    limit: int,
    cursor: Cursor | None = None,
    status: PaymentStatus | None = None,
) -> Page:
    conditions = [Payment.merchant_id == merchant_id]
    if status is not None:
        conditions.append(Payment.status == status)
    if cursor is not None:
        # (created_at, id) < (cursor.created_at, cursor.id), newest first
        conditions.append(
            or_(
                Payment.created_at < cursor.created_at,
                and_(Payment.created_at == cursor.created_at, Payment.id < cursor.id),
            )
        )
    async with SessionLocal() as session:
        rows = list(
            await session.scalars(
                select(Payment)
                .where(*conditions)
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .limit(limit + 1)  # one extra row tells us whether another page exists
            )
        )
    has_more = len(rows) > limit
    data = rows[:limit]
    next_cursor = (
        encode_cursor(Cursor(created_at=data[-1].created_at, id=data[-1].id))
        if has_more and data
        else None
    )
    return Page(data=data, has_more=has_more, next_cursor=next_cursor)
