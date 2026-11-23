"""Webhooks FROM the bank (e.g. "the UPI payment you started was approved").

Rules for receiving webhooks, the mirror image of sending them:
1. Verify the signature over the raw body before trusting anything (done in the API).
2. Dedupe by the sender's event id: banks resend events. The (provider, event_id)
   primary key of inbound_webhooks makes the second copy a duplicate-key error.
3. Record the event and apply it in ONE transaction: if applying fails, the record
   rolls back too, so the bank's retry is processed rather than skipped as a duplicate.
4. Answer fast. Here applying is a few SQL statements; slow work would be queued.
"""

from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from payforge.clients import simbank
from payforge.db import SessionLocal
from payforge.logging import log
from payforge.models import InboundWebhook, Payment, PaymentStatus
from payforge.services import payments
from payforge.services.ledger import forget_balance

Outcome = Literal["processed", "ignored", "duplicate"]


def _result(outcome: simbank.ChargeOutcome) -> payments.PaymentResult | None:
    match outcome:
        case simbank.Approved(charge_id=charge_id):
            return payments.PaymentSucceeded(bank_charge_id=charge_id)
        case simbank.Declined(charge_id=charge_id, decline_code=code):
            return payments.PaymentFailed(failure_code=code, bank_charge_id=charge_id)
        case _:
            return None  # still pending: nothing to do yet


async def handle_charge_update(update: simbank.ChargeUpdate) -> Outcome:
    result = _result(update.outcome)
    async with SessionLocal.begin() as session:
        exists = await session.scalar(select(Payment.id).where(Payment.id == update.reference))
        outcome: Outcome = "processed" if exists and result else "ignored"
        try:
            async with session.begin_nested():  # a savepoint: only this insert is undone
                session.add(
                    InboundWebhook(
                        provider="simbank",
                        event_id=update.event_id,
                        type=update.type,
                        payload=update.payload,
                        outcome=outcome,
                    )
                )
        except IntegrityError:
            log.info("bank_webhook_duplicate", event_id=update.event_id)
            return "duplicate"
        if outcome == "ignored" or result is None:
            log.warning("bank_webhook_ignored", event_id=update.event_id, ref=update.reference)
            return "ignored"
        payment = await payments.apply_payment_result(session, update.reference, result)
    if payment is not None and payment.status == PaymentStatus.SUCCEEDED:
        await forget_balance(payment.merchant_id)  # after COMMIT
    return "processed"
