"""Idempotency keys: a retried POST returns the first response instead of acting twice.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "merchant_id",
            sa.String(40),
            sa.ForeignKey("merchants.id"),
            primary_key=True,
        ),
        # Binary collation: the default one ignores case and accents, which would make
        # "Order-1" and "order-1" the same key. Keys are ASCII (checked by the API).
        sa.Column(
            "idempotency_key",
            VARCHAR(255, charset="ascii", collation="ascii_bin"),
            primary_key=True,
        ),
        sa.Column("request_hash", sa.String(64), nullable=False),  # sha-256 of the request
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("resource_id", sa.String(40)),  # the payment/refund this key created
        sa.Column("response_status", sa.SmallInteger),
        sa.Column("response_body", sa.JSON),
        # whoever holds lock_token is working on this key; locked_at tells when it
        # started, so a crashed worker's lock can be taken over
        sa.Column("lock_token", sa.String(32)),
        sa.Column("locked_at", DATETIME(fsp=3)),
        sa.Column(
            "created_at",
            DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
        ),
        sa.Column("expires_at", DATETIME(fsp=3), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')", name="idempotency_keys_status_known"
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR "
            "(response_status IS NOT NULL AND response_body IS NOT NULL AND lock_token IS NULL)",
            name="idempotency_keys_completed_has_response",
        ),
    )
    op.create_index("idempotency_keys_expires_idx", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    op.drop_table("idempotency_keys")
