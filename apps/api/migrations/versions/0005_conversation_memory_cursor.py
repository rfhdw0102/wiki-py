"""Track the last conversation message incorporated into compact memory.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("memory_cursor_id", sa.String(36), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "memory_cursor_id")
