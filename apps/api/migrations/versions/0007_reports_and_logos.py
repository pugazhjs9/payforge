"""Reports (CSV exports built in the background, stored in S3) and merchant logos.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("params", sa.JSON, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("file_key", sa.String(200)),
        sa.Column("row_count", sa.Integer),
        sa.Column("error", sa.String(300)),
        sa.Column(
            "created_at",
            DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
        ),
        sa.Column("started_at", DATETIME(fsp=3)),
        sa.Column("finished_at", DATETIME(fsp=3)),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'ready', 'failed')", name="reports_status_known"
        ),
        sa.CheckConstraint("type IN ('payments')", name="reports_type_known"),
    )
    # the worker's queue: oldest pending first
    op.create_index("reports_status_created_idx", "reports", ["status", "created_at"])
    op.create_index("reports_merchant_idx", "reports", ["merchant_id"])

    op.add_column("merchants", sa.Column("logo_key", sa.String(200)))


def downgrade() -> None:
    op.drop_column("merchants", "logo_key")
    op.drop_table("reports")
