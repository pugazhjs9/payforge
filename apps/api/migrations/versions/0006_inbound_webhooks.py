"""Webhooks received from the bank: an audit log that also dedupes them.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inbound_webhooks",
        # the sender's event id is the key: the same event sent twice is stored once
        sa.Column("provider", sa.String(20), primary_key=True),
        sa.Column("event_id", sa.String(100), primary_key=True),
        sa.Column("type", sa.String(50), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),  # processed | ignored
        sa.Column(
            "received_at",
            DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
        ),
    )


def downgrade() -> None:
    op.drop_table("inbound_webhooks")
