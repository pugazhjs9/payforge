"""Request and response shapes for API v1.

Unknown request fields are rejected (extra="forbid"), so a typo like "ammount" fails
loudly instead of being ignored. Amount FORMAT is checked by to_minor(), which gives
a precise message. Responses never expose internal names (amount_minor…), so the
database can change without breaking the API contract.
"""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from payforge.domain.money import format_minor
from payforge.models import Event, Payment, PaymentStatus, Refund, RefundStatus

Amount = Annotated[str, Field(max_length=20, examples=["499.00"])]
MetadataKey = Annotated[str, StringConstraints(min_length=1, max_length=40)]
MetadataValue = Annotated[str, StringConstraints(max_length=500)]


class CreatePayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Amount
    currency: Literal["INR"]
    payment_method: str = Field(
        min_length=1,
        max_length=100,
        description=(
            "Test tokens: tok_card_ok, tok_card_declined, tok_card_no_funds, "
            "tok_card_slow (answers after our timeout), tok_card_no_refunds."
        ),
    )
    description: str | None = Field(default=None, max_length=200)
    customer_email: EmailStr | None = None
    metadata: dict[MetadataKey, MetadataValue] | None = Field(default=None, max_length=20)


class CreateRefund(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Amount | None = Field(default=None, description="Omit to refund everything left.")
    reason: str | None = Field(default=None, max_length=200)


class PaymentMethodOut(BaseModel):
    type: str
    token: str


class PaymentOut(BaseModel):
    id: str = Field(examples=["pay_01JD3W7K8Y8M0F2T6QH5RJZC4N"])
    object: Literal["payment"] = "payment"
    amount: str = Field(examples=["499.00"])
    amount_refunded: str = Field(description="Includes refunds still pending.")
    fee: str = Field(examples=["9.98"])
    currency: str
    status: PaymentStatus
    payment_method: PaymentMethodOut
    failure_code: str | None
    description: str | None
    customer_email: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, p: Payment) -> "PaymentOut":
        return cls(
            id=p.id,
            amount=format_minor(p.amount_minor),
            amount_refunded=format_minor(p.amount_refunded_minor),
            fee=format_minor(p.fee_minor),
            currency=p.currency,
            status=p.status,
            payment_method=PaymentMethodOut(type=p.method, token=p.method_token),
            failure_code=p.failure_code,
            description=p.description,
            customer_email=p.customer_email,
            metadata=p.metadata_,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


class RefundOut(BaseModel):
    id: str
    object: Literal["refund"] = "refund"
    payment_id: str
    amount: str
    status: RefundStatus
    reason: str | None
    failure_code: str | None
    created_at: datetime

    @classmethod
    def of(cls, r: Refund) -> "RefundOut":
        return cls(
            id=r.id,
            payment_id=r.payment_id,
            amount=format_minor(r.amount_minor),
            status=r.status,
            reason=r.reason,
            failure_code=r.failure_code,
            created_at=r.created_at,
        )


class PaymentList(BaseModel):
    object: Literal["list"] = "list"
    data: list[PaymentOut]
    has_more: bool
    next_cursor: str | None = Field(description="Pass as ?cursor= to get the next page.")


class RefundList(BaseModel):
    object: Literal["list"] = "list"
    data: list[RefundOut]
    has_more: bool = False
    next_cursor: str | None = None


class BalanceOut(BaseModel):
    object: Literal["balance"] = "balance"
    currency: Literal["INR"] = "INR"
    available: str = Field(description="Can be negative: refund fees are not returned.")


class EventData(BaseModel):
    object: dict[str, Any] = Field(description="The payment or refund, as it was at that moment.")


class EventOut(BaseModel):
    """Also the body of every webhook PayForge sends."""

    id: str = Field(examples=["evt_01JD3W7K8Y8M0F2T6QH5RJZC4N"])
    object: Literal["event"] = "event"
    type: str = Field(examples=["payment.succeeded"])
    created_at: datetime
    data: EventData

    @classmethod
    def of(cls, e: Event) -> "EventOut":
        return cls(id=e.id, type=e.type, created_at=e.created_at, data=EventData(object=e.data))


class EventList(BaseModel):
    object: Literal["list"] = "list"
    data: list[EventOut]
    has_more: bool
    next_cursor: str | None
