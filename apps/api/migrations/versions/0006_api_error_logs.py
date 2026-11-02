"""Persist API errors for the personal runtime log.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_error_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("error_type", sa.String(length=256), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_error_logs_user_id", "api_error_logs", ["user_id"])
    op.create_index("ix_api_error_logs_created_at", "api_error_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_api_error_logs_created_at", table_name="api_error_logs")
    op.drop_index("ix_api_error_logs_user_id", table_name="api_error_logs")
    op.drop_table("api_error_logs")
