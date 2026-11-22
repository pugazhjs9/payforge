from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from payforge.api.deps import CurrentMerchant
from payforge.api.v1.schemas import (
    CreatePayment,
    PaymentList,
    PaymentOut,
)
from payforge.domain.cursor import decode_cursor
from payforge.models import PaymentStatus
from payforge.services import payments

router = APIRouter()

ACCEPTED: dict[int | str, dict[str, Any]] = {
    202: {"description": "Created, but the bank's answer is not known yet."}
}


@router.post("/payments", status_code=201, responses=ACCEPTED, tags=["Payments"])
async def create_payment(
    body: CreatePayment, merchant: CurrentMerchant, response: Response
) -> PaymentOut:
    """Charge a payment method through the bank.

    **201**: the bank's answer is known (`succeeded`, or `failed` for a decline).
    **202**: not known yet (`processing`, e.g. the bank timed out). A timeout never means failed.
    """
    payment, final = await payments.create_payment(
        merchant,
        payments.NewPayment(
            amount=body.amount,
            currency=body.currency,
            payment_method=body.payment_method,
            description=body.description,
            customer_email=str(body.customer_email) if body.customer_email else None,
            metadata=body.metadata,
        ),
    )
    if not final:
        response.status_code = 202
    return PaymentOut.of(payment)


@router.get("/payments", tags=["Payments"])
async def list_payments(
    merchant: CurrentMerchant,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[
        str | None, Query(max_length=500, description="next_cursor from the previous page")
    ] = None,
    status: PaymentStatus | None = None,
) -> PaymentList:
    """List payments, newest first, with cursor pagination."""
    page = await payments.list_payments(
        merchant.id,
        limit=limit,
        cursor=decode_cursor(cursor) if cursor else None,
        status=status,
    )
    return PaymentList(
        data=[PaymentOut.of(p) for p in page.data],
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


@router.get("/payments/{payment_id}", tags=["Payments"])
async def get_payment(payment_id: str, merchant: CurrentMerchant) -> PaymentOut:
    return PaymentOut.of(await payments.get_payment(merchant.id, payment_id))
