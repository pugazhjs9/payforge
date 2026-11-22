from typing import Any

from fastapi import APIRouter, Response

from payforge.api.deps import CurrentMerchant
from payforge.api.v1.schemas import CreatePayment, PaymentOut
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


@router.get("/payments/{payment_id}", tags=["Payments"])
async def get_payment(payment_id: str, merchant: CurrentMerchant) -> PaymentOut:
    return PaymentOut.of(await payments.get_payment(merchant.id, payment_id))
