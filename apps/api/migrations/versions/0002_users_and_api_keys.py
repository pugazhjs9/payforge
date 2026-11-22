"""Users (dashboard logins) and API keys (merchant servers).

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("github_id", sa.String(40), unique=True),
        sa.Column("name", sa.String(100)),
        sa.Column("role", sa.String(20), nullable=False, server_default="owner"),
        sa.Column(
            "created_at",
            DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
        ),
        sa.CheckConstraint("role IN ('owner', 'developer', 'viewer')", name="users_role_known"),
        # The default collation is case-INsensitive, so compare the raw bytes: emails are
        # always stored lowercased (and the unique index already ignores case).
        sa.CheckConstraint(
            "CAST(email AS BINARY) = CAST(LOWER(email) AS BINARY)", name="users_email_lowercase"
        ),
        # a user must be able to log in somehow
        sa.CheckConstraint(
            "password_hash IS NOT NULL OR github_id IS NOT NULL", name="users_has_login_method"
        ),
    )
    op.create_index("users_merchant_idx", "users", ["merchant_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("merchant_id", sa.String(40), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("secret_last4", sa.String(4), nullable=False),
        sa.Column("scopes", sa.JSON, nullable=False),
        sa.Column(
            "created_at",
            DATETIME(fsp=3),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP(3)"),
        ),
        sa.Column("last_used_at", DATETIME(fsp=3)),
        sa.Column("revoked_at", DATETIME(fsp=3)),
    )
    op.create_index("api_keys_merchant_idx", "api_keys", ["merchant_id"])


def downgrade() -> None:
    op.drop_table("api_keys")
    op.drop_table("users")
