from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from payforge.api.deps import PaymentsRead, PaymentsWrite, RefundsWrite
from payforge.api.idempotency import IdempotencyKey, run_once
from payforge.api.v1.schemas import (
    CreatePayment,
    CreateRefund,
    PaymentList,
    PaymentOut,
    RefundList,
    RefundOut,
)
from payforge.domain.cursor import decode_cursor
from payforge.models import PaymentStatus
from payforge.services import payments, refunds

router = APIRouter()

CREATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {"description": "Created, but the bank's answer is not known yet."},
    400: {"description": "No Idempotency-Key header."},
    409: {"description": "A request with this Idempotency-Key is still running: retry."},
    422: {"description": "Invalid request, or the Idempotency-Key was used for another one."},
}


@router.post(
    "/payments",
    status_code=201,
    response_model=PaymentOut,
    responses=CREATE_RESPONSES,
    tags=["Payments"],
)
async def create_payment(
    body: CreatePayment,
    merchant: PaymentsWrite,
    key: IdempotencyKey,
    request: Request,
    response: Response,
) -> Response:
    """Charge a payment method through the bank.

    **201**: the bank's answer is known (`succeeded`, or `failed` for a decline).
    **202**: not known yet (`processing`, e.g. the bank timed out). A timeout never means failed.

    Requires an `Idempotency-Key`: a retry with the same key returns the first response
    (with `Idempotent-Replayed: true`) and never charges twice.
    """
    data = payments.NewPayment(
        amount=body.amount,
        currency=body.currency,
        payment_method=body.payment_method,
        description=body.description,
        customer_email=str(body.customer_email) if body.customer_email else None,
        metadata=body.metadata,
    )

    async def create(session: AsyncSession) -> str:
        return (await payments.insert_payment(session, merchant, data)).id

    async def finish(payment_id: str) -> tuple[int, PaymentOut]:
        payment, final = await payments.process_payment(await payments.load_payment(payment_id))
        return (201 if final else 202), PaymentOut.of(payment)

    return await run_once(
        request=request,
        response=response,
        merchant_id=merchant.id,
        key=key,
        body=body,
        create=create,
        finish=finish,
    )


@router.get("/payments", tags=["Payments"])
async def list_payments(
    merchant: PaymentsRead,
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
async def get_payment(payment_id: str, merchant: PaymentsRead) -> PaymentOut:
    return PaymentOut.of(await payments.get_payment(merchant.id, payment_id))


@router.post(
    "/payments/{payment_id}/refunds",
    status_code=201,
    response_model=RefundOut,
    responses=CREATE_RESPONSES,
    tags=["Refunds"],
)
async def create_refund(
    payment_id: str,
    merchant: RefundsWrite,
    key: IdempotencyKey,
    request: Request,
    response: Response,
    body: CreateRefund | None = None,
) -> Response:
    """Refund all (no body, or no `amount`) or part of a payment. Requires an
    `Idempotency-Key`, like creating a payment."""
    body = body or CreateRefund()
    data = refunds.NewRefund(amount=body.amount, reason=body.reason)

    async def create(session: AsyncSession) -> str:
        return (await refunds.reserve_refund(session, merchant.id, payment_id, data)).id

    async def finish(refund_id: str) -> tuple[int, RefundOut]:
        refund, final = await refunds.process_refund(await refunds.load_refund(refund_id))
        return (201 if final else 202), RefundOut.of(refund)

    return await run_once(
        request=request,
        response=response,
        merchant_id=merchant.id,
        key=key,
        body=body,
        create=create,
        finish=finish,
    )


@router.get("/payments/{payment_id}/refunds", tags=["Refunds"])
async def list_refunds(payment_id: str, merchant: PaymentsRead) -> RefundList:
    return RefundList(
        data=[RefundOut.of(r) for r in await refunds.list_refunds(merchant.id, payment_id)]
    )
