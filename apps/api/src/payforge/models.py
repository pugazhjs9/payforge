"""PayForge data model (SQLAlchemy 2.0).

The tables themselves are created by hand-written Alembic migrations, which also
add the CHECK constraints and ledger triggers the database enforces.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, Enum, ForeignKey, Integer, String
from sqlalchemy.dialects.mysql import DATETIME
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
