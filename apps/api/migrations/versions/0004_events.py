"""Events: the transactional outbox.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.String(40), nullable=False),
        sa.Column("data", sa.JSON, nullable=False),  # snapshot of the resource, as the API shows it
        sa.Column(
            "created_at",
            DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
        ),
        sa.Column("published_at", DATETIME(fsp=3)),  # NULL until the relay sent it to RabbitMQ
    )
    # GET /v1/events: newest first per merchant
    op.execute(
        "CREATE INDEX events_merchant_created_idx ON events (merchant_id, created_at DESC, id DESC)"
    )
    # the relay: oldest unpublished first
    op.create_index("events_unpublished_idx", "events", ["published_at", "created_at", "id"])


def downgrade() -> None:
    op.drop_table("events")
