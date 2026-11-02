"""Persist scoped question conversations and tool-calling runs.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wiki_id",
            sa.String(36),
            sa.ForeignKey("wikis.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("topic_memory", sa.Text(), nullable=False),
        sa.Column("active_entities", sa.JSON(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_wiki_id", "conversations", ["wiki_id"])
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("answer_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id",
        "conversation_messages",
        ["conversation_id"],
    )
    op.create_table(
        "question_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wiki_id",
            sa.String(36),
            sa.ForeignKey("wikis.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=False),
        sa.Column("model_profile_id", sa.String(36), nullable=False),
        sa.Column("question", sa.String(1000), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=False),
        sa.Column("standalone_question", sa.String(1000), nullable=True),
        sa.Column("active_entities", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("tool_trace", sa.JSON(), nullable=False),
        sa.Column("answer_json", sa.JSON(), nullable=True),
        sa.Column("message_id", sa.String(36), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *timestamps(),
        sa.UniqueConstraint("conversation_id", "request_id"),
        sa.UniqueConstraint("user_id", "wiki_id", "request_id"),
    )
    for field in ("conversation_id", "user_id", "wiki_id"):
        op.create_index(f"ix_question_runs_{field}", "question_runs", [field])


def downgrade() -> None:
    op.drop_table("question_runs")
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
