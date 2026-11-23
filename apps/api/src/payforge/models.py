"""PayForge data model (SQLAlchemy 2.0).

The tables themselves are created by hand-written Alembic migrations, which also
add the CHECK constraints and ledger triggers the database enforces.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    """Now, in UTC, trimmed to milliseconds: DATETIME(3) rounds anything finer, and the
    value in memory must equal the stored one (pagination cursors compare them)."""
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


class UTCDateTime(TypeDecorator[datetime]):
    """MySQL DATETIME(3) holding UTC. Python always sees timezone-aware datetimes."""

    impl = DATETIME(fsp=3)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime: use timezone-aware UTC datetimes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def str_enum(enum: type[StrEnum], name: str) -> Enum:
    # VARCHAR + CHECK instead of a native MySQL ENUM: adding a value later is a
    # plain migration, not a table rebuild.
    return Enum(
        enum,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda e: [m.value for m in e],
    )


class PaymentStatus(StrEnum):
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RefundStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class DeliveryStatus(StrEnum):
    PENDING = "pending"  # waiting for (another) attempt
    SUCCEEDED = "succeeded"  # the endpoint answered 2xx
    FAILED = "failed"  # gave up after the last retry
    CANCELED = "canceled"  # the endpoint was deleted


class UserRole(StrEnum):
    OWNER = "owner"  # everything
    DEVELOPER = "developer"  # API keys, read payments
    VIEWER = "viewer"  # read only


class Base(DeclarativeBase):
    pass


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    fee_bps: Mapped[int] = mapped_column(Integer, default=200)  # 200 basis points = 2%
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("fee_bps BETWEEN 0 AND 10000", name="merchants_fee_bps_range"),
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # pay_<ulid>
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    amount_minor: Mapped[int] = mapped_column(Integer)  # paise
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus, "payment_status"), default=PaymentStatus.PROCESSING
    )
    method: Mapped[str] = mapped_column(String(10))  # card | upi
    method_token: Mapped[str] = mapped_column(String(100))
    failure_code: Mapped[str | None] = mapped_column(String(50))
    description: Mapped[str | None] = mapped_column(String(200))
    customer_email: Mapped[str | None] = mapped_column(String(254))
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    fee_minor: Mapped[int] = mapped_column(Integer, default=0)
    amount_refunded_minor: Mapped[int] = mapped_column(
        Integer, default=0
    )  # includes pending refunds
    bank_charge_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    merchant: Mapped[Merchant] = relationship(lazy="raise")


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # re_<ulid>
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"))
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    amount_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[RefundStatus] = mapped_column(
        str_enum(RefundStatus, "refund_status"), default=RefundStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(String(200))
    failure_code: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class LedgerTransaction(Base):
    """A group of transfers posted together. The unique key makes posting idempotent."""

    __tablename__ = "ledger_transactions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # ltx_<ulid>
    transaction_key: Mapped[str] = mapped_column(String(100), unique=True)  # payment:pay_…
    merchant_id: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class LedgerTransfer(Base):
    """Money moving FROM one account TO another. Each row balances by itself, so the
    books can never be unbalanced. Append-only (enforced by triggers)."""

    __tablename__ = "ledger_transfers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("ledger_transactions.id"))
    from_account: Mapped[str] = mapped_column(String(80))
    to_account: Mapped[str] = mapped_column(String(80))
    amount_minor: Mapped[int] = mapped_column(Integer)  # always > 0
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(Base):
    """A person who uses the dashboard. Logs in with a password and/or GitHub."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # usr_<ulid>
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    email: Mapped[str] = mapped_column(String(254), unique=True)  # stored lowercased
    password_hash: Mapped[str | None] = mapped_column(String(255))  # argon2id; None = GitHub only
    github_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    name: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[UserRole] = mapped_column(str_enum(UserRole, "user_role"), default=UserRole.OWNER)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ApiKey(Base):
    """Credentials for a merchant's server. The secret is shown once; only its hash is stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(40), primary_key=True
    )  # key_<ulid>: public, like a username
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    name: Mapped[str] = mapped_column(String(100))
    secret_hash: Mapped[str] = mapped_column(String(64))  # sha-256 hex of the secret
    secret_last4: Mapped[str] = mapped_column(String(4))  # to recognise a key in the dashboard
    scopes: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class IdempotencyKey(Base):
    """One Idempotency-Key a merchant sent: which request it was for, the resource it
    created, and the response to replay when the same request is retried."""

    __tablename__ = "idempotency_keys"

    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), primary_key=True)
    key: Mapped[str] = mapped_column(
        "idempotency_key", VARCHAR(255, charset="ascii", collation="ascii_bin"), primary_key=True
    )
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[IdempotencyStatus] = mapped_column(
        str_enum(IdempotencyStatus, "idempotency_status"), default=IdempotencyStatus.IN_PROGRESS
    )
    resource_id: Mapped[str | None] = mapped_column(String(40))
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    lock_token: Mapped[str | None] = mapped_column(String(32))
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Event(Base):
    """Something that happened to a resource (payment.succeeded…). Written in the same
    transaction as the change itself, then published by the relay (transactional outbox)."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # evt_<ulid>
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    type: Mapped[str] = mapped_column(String(50))
    resource_id: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class WebhookEndpoint(Base):
    """A URL on the merchant's side that receives events, signed with `secret`."""

    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # we_<ulid>
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    url: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(String(100))
    secret: Mapped[str] = mapped_column(String(64))  # whsec_…
    event_types: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    def wants(self, event_type: str) -> bool:
        return "*" in self.event_types or event_type in self.event_types


class WebhookDelivery(Base):
    """One event going to one endpoint, over one or more attempts."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("event_id", "endpoint_id"),)

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # wd_<ulid>
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"))
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("webhook_endpoints.id"))
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"))
    status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus, "delivery_status"), default=DeliveryStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_status_code: Mapped[int | None] = mapped_column(SmallInteger)
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class WebhookAttempt(Base):
    """The log line of one HTTP attempt, shown in the dashboard."""

    __tablename__ = "webhook_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("webhook_deliveries.id"))
    number: Mapped[int] = mapped_column(Integer)
    status_code: Mapped[int | None] = mapped_column(SmallInteger)
    error: Mapped[str | None] = mapped_column(String(300))
    duration_ms: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class InboundWebhook(Base):
    """A webhook the bank sent us. The (provider, event_id) key turns a resent event
    into a duplicate-key error, so it is processed once."""

    __tablename__ = "inbound_webhooks"

    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(20))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
