"""Support a username-based default account and optional email binding.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(length=320), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute("UPDATE users SET username = email WHERE username IS NULL")
    op.alter_column("users", "email", existing_type=sa.String(length=320), nullable=True)
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.alter_column("users", "must_change_password", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_users_username", table_name="users")
    op.execute(
        "UPDATE users SET email = 'unbound-' || id || '@deleted.invalid' WHERE email IS NULL"
    )
    op.alter_column("users", "email", existing_type=sa.String(length=320), nullable=False)
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "username")
