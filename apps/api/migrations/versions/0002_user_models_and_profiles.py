"""Scope models to users and add account avatars.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_path", sa.String(1024), nullable=True))
    op.add_column(
        "source_spans",
        sa.Column("embedding_profile_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "wiki_index_state",
        sa.Column("embedding_profile_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "model_profiles",
        sa.Column("owner_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "model_profiles",
        sa.Column(
            "provider",
            sa.String(64),
            nullable=False,
            server_default="custom",
        ),
    )
    op.execute(
        """
        UPDATE model_profiles AS profile
        SET owner_id = COALESCE(
            (
                SELECT audit.actor_id
                FROM audit_logs AS audit
                WHERE audit.action = 'model_profile.created'
                  AND audit.resource_id = profile.id
                  AND audit.actor_id IS NOT NULL
                ORDER BY audit.created_at
                LIMIT 1
            ),
            (
                SELECT users.id
                FROM users
                ORDER BY CASE WHEN users.role = 'ADMIN' THEN 0 ELSE 1 END,
                         users.created_at
                LIMIT 1
            )
        )
        """
    )
    op.alter_column("model_profiles", "owner_id", nullable=False)
    op.create_foreign_key(
        "fk_model_profiles_owner_id",
        "model_profiles",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_model_profiles_owner_id",
        "model_profiles",
        ["owner_id"],
    )
    op.drop_constraint("model_profiles_name_key", "model_profiles", type_="unique")
    op.create_unique_constraint(
        "uq_model_profiles_owner_name",
        "model_profiles",
        ["owner_id", "name"],
    )

    op.add_column(
        "model_assignments",
        sa.Column("user_id", sa.String(36), nullable=True),
    )
    op.execute(
        """
        UPDATE model_assignments AS assignment
        SET user_id = profile.owner_id
        FROM model_profiles AS profile
        WHERE assignment.model_profile_id = profile.id
        """
    )
    op.alter_column("model_assignments", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_model_assignments_user_id",
        "model_assignments",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("model_assignments_pkey", "model_assignments", type_="primary")
    op.create_primary_key(
        "pk_model_assignments",
        "model_assignments",
        ["user_id", "purpose"],
    )


def downgrade() -> None:
    op.drop_constraint("pk_model_assignments", "model_assignments", type_="primary")
    op.drop_constraint(
        "fk_model_assignments_user_id",
        "model_assignments",
        type_="foreignkey",
    )
    op.drop_column("model_assignments", "user_id")
    op.create_primary_key("model_assignments_pkey", "model_assignments", ["purpose"])

    op.drop_constraint(
        "uq_model_profiles_owner_name",
        "model_profiles",
        type_="unique",
    )
    op.create_unique_constraint(
        "model_profiles_name_key",
        "model_profiles",
        ["name"],
    )
    op.drop_index("ix_model_profiles_owner_id", table_name="model_profiles")
    op.drop_constraint(
        "fk_model_profiles_owner_id",
        "model_profiles",
        type_="foreignkey",
    )
    op.drop_column("model_profiles", "owner_id")
    op.drop_column("model_profiles", "provider")
    op.drop_column("wiki_index_state", "embedding_profile_id")
    op.drop_column("source_spans", "embedding_profile_id")
    op.drop_column("users", "avatar_path")
