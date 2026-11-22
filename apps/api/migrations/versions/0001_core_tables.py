"""Core tables: merchants, payments, refunds and the double-entry ledger.

Besides the tables, this migration adds rules MySQL enforces even if application
code has a bug: CHECK constraints on money columns, and triggers that make ledger
transfers append-only.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def ts(name: str) -> sa.Column[object]:
    return sa.Column(
        name, DATETIME(fsp=3), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(3)")
    )


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("fee_bps", sa.Integer, nullable=False, server_default="200"),
        ts("created_at"),
        sa.CheckConstraint("fee_bps BETWEEN 0 AND 10000", name="merchants_fee_bps_range"),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("amount_minor", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="processing"),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("method_token", sa.String(100), nullable=False),
        sa.Column("failure_code", sa.String(50)),
        sa.Column("description", sa.String(200)),
        sa.Column("customer_email", sa.String(254)),
        sa.Column("metadata", sa.JSON, nullable=False),
        sa.Column("fee_minor", sa.Integer, nullable=False, server_default="0"),
        sa.Column("amount_refunded_minor", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bank_charge_id", sa.String(100)),
        ts("created_at"),
        ts("updated_at"),
        sa.CheckConstraint(
            "status IN ('processing', 'succeeded', 'failed')", name="payments_status_known"
        ),
        sa.CheckConstraint("amount_minor BETWEEN 100 AND 50000000", name="payments_amount_range"),
        sa.CheckConstraint("currency = 'INR'", name="payments_currency_inr"),
        sa.CheckConstraint("method IN ('card', 'upi')", name="payments_method_known"),
        sa.CheckConstraint("fee_minor BETWEEN 0 AND amount_minor", name="payments_fee_range"),
        # the total of all refunds (including pending ones) can never exceed the payment
        sa.CheckConstraint(
            "amount_refunded_minor BETWEEN 0 AND amount_minor", name="payments_refunded_range"
        ),
    )
    # newest-first listing per merchant (cursor pagination)
    op.execute(
        "CREATE INDEX payments_merchant_created_idx "
        "ON payments (merchant_id, created_at DESC, id DESC)"
    )

    op.create_table(
        "refunds",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("payment_id", sa.String(40), sa.ForeignKey("payments.id"), nullable=False),
        sa.Column("merchant_id", sa.String(40), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("amount_minor", sa.Integer, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reason", sa.String(200)),
        sa.Column("failure_code", sa.String(50)),
        ts("created_at"),
        ts("updated_at"),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')", name="refunds_status_known"
        ),
        sa.CheckConstraint("amount_minor > 0", name="refunds_amount_positive"),
    )
    op.create_index("refunds_payment_created_idx", "refunds", ["payment_id", "created_at"])

    op.create_table(
        "ledger_transactions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("transaction_key", sa.String(100), nullable=False, unique=True),
        sa.Column("merchant_id", sa.String(40), nullable=False),
        ts("created_at"),
    )

    # Double-entry as transfers: every row moves money FROM one account TO another,
    # so each row balances by itself and the books can never be unbalanced.
    op.create_table(
        "ledger_transfers",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "transaction_id",
            sa.String(40),
            sa.ForeignKey("ledger_transactions.id"),
            nullable=False,
        ),
        sa.Column("from_account", sa.String(80), nullable=False),
        sa.Column("to_account", sa.String(80), nullable=False),
        sa.Column("amount_minor", sa.Integer, nullable=False),
        ts("created_at"),
        sa.CheckConstraint("amount_minor > 0", name="ledger_transfers_amount_positive"),
        sa.CheckConstraint("from_account <> to_account", name="ledger_transfers_two_accounts"),
    )
    op.create_index("ledger_transfers_from_idx", "ledger_transfers", ["from_account"])
    op.create_index("ledger_transfers_to_idx", "ledger_transfers", ["to_account"])

    # Append-only: a mistake is fixed with a new, reversing transfer, never by editing.
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER ledger_transfers_no_{event.lower()} "
            f"BEFORE {event} ON ledger_transfers FOR EACH ROW "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ledger_transfers is append-only'"
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_transfers_no_delete")
    op.execute("DROP TRIGGER IF EXISTS ledger_transfers_no_update")
    op.drop_table("ledger_transfers")
    op.drop_table("ledger_transactions")
    op.drop_table("refunds")
    op.drop_table("payments")
    op.drop_table("merchants")
