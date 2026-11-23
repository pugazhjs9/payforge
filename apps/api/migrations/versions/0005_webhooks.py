"""Webhook endpoints, deliveries and the log of every delivery attempt.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def ts(name: str, nullable: bool = False) -> sa.Column[object]:
    if nullable:
        return sa.Column(name, DATETIME(fsp=3))
    return sa.Column(
        name, DATETIME(fsp=3), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(3)")
    )


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("description", sa.String(100)),
        sa.Column("secret", sa.String(64), nullable=False),  # signs every delivery
        sa.Column("event_types", sa.JSON, nullable=False),  # ["*"] = every type
        ts("created_at"),
        ts("deleted_at", nullable=True),
    )
    op.create_index("webhook_endpoints_merchant_idx", "webhook_endpoints", ["merchant_id"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column(
            "endpoint_id", sa.String(40), sa.ForeignKey("webhook_endpoints.id"), nullable=False
        ),
        sa.Column("event_id", sa.String(40), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        # when the next attempt is due; while one runs, a lease (see services/webhooks.py)
        ts("next_attempt_at", nullable=True),
        ts("last_attempt_at", nullable=True),
        sa.Column("last_status_code", sa.SmallInteger),
        sa.Column("last_error", sa.String(300)),
        ts("created_at"),
        # an event goes to an endpoint once, even if RabbitMQ hands us the event twice
        sa.UniqueConstraint("event_id", "endpoint_id", name="webhook_deliveries_event_endpoint"),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'canceled')",
            name="webhook_deliveries_status_known",
        ),
        sa.CheckConstraint("attempts >= 0", name="webhook_deliveries_attempts_positive"),
    )
    op.execute(
        "CREATE INDEX webhook_deliveries_endpoint_created_idx "
        "ON webhook_deliveries (endpoint_id, created_at DESC, id DESC)"
    )
    # the sweeper: pending deliveries whose next attempt is overdue
    op.create_index(
        "webhook_deliveries_due_idx", "webhook_deliveries", ["status", "next_attempt_at"]
    )

    op.create_table(
        "webhook_attempts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "delivery_id", sa.String(40), sa.ForeignKey("webhook_deliveries.id"), nullable=False
        ),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("status_code", sa.SmallInteger),  # NULL: no HTTP response at all
        sa.Column("error", sa.String(300)),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("response_body", sa.String(500)),  # the start of it, for debugging
        ts("created_at"),
        sa.UniqueConstraint("delivery_id", "number", name="webhook_attempts_delivery_number"),
    )


def downgrade() -> None:
    op.drop_table("webhook_attempts")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_endpoints")
