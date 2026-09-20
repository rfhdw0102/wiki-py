"""Initial core tables.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    user_role_type = postgresql.ENUM("ADMIN", "USER", name="userrole")
    source_kind_type = postgresql.ENUM(
        "PDF", "DOCX", "MARKDOWN", "TEXT", "HTML", "WEBSITE", name="sourcekind"
    )
    draft_status_type = postgresql.ENUM(
        "OPEN", "PUBLISHED", "REJECTED", name="draftstatus"
    )
    model_kind_type = postgresql.ENUM(
        "CHAT", "EMBEDDING", "RERANKER", name="modelkind"
    )
    user_role_type.create(op.get_bind())
    source_kind_type.create(op.get_bind())
    draft_status_type.create(op.get_bind())
    model_kind_type.create(op.get_bind())
    user_role = postgresql.ENUM("ADMIN", "USER", name="userrole", create_type=False)
    source_kind = postgresql.ENUM(
        "PDF",
        "DOCX",
        "MARKDOWN",
        "TEXT",
        "HTML",
        "WEBSITE",
        name="sourcekind",
        create_type=False,
    )
    draft_status = postgresql.ENUM(
        "OPEN", "PUBLISHED", "REJECTED", name="draftstatus", create_type=False
    )
    model_kind = postgresql.ENUM(
        "CHAT", "EMBEDDING", "RERANKER", name="modelkind", create_type=False
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("refresh_token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        *timestamps(),
    )
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("kind", model_kind, nullable=False),
        sa.Column("base_url", sa.String(1024), nullable=False),
        sa.Column("model_name", sa.String(256), nullable=False),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        *timestamps(),
    )
    op.create_table(
        "model_assignments",
        sa.Column("purpose", sa.String(64), primary_key=True),
        sa.Column(
            "model_profile_id",
            sa.String(36),
            sa.ForeignKey("model_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *timestamps(),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("kind", source_kind, nullable=False),
        sa.Column("created_by_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("trust_level", sa.Integer(), nullable=False),
        *timestamps(),
    )
    op.create_index("ix_sources_created_by_id", "sources", ["created_by_id"])
    op.create_table(
        "source_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_id", sa.String(36), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_path", sa.String(1024), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("source_id", "sha256"),
    )
    op.create_index("ix_source_revisions_source_id", "source_revisions", ["source_id"])
    op.create_index("ix_source_revisions_sha256", "source_revisions", ["sha256"])
    op.create_table(
        "source_spans",
        sa.Column("id", sa.String(192), primary_key=True),
        sa.Column(
            "source_revision_id",
            sa.String(36),
            sa.ForeignKey("source_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(512), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("locator", sa.JSON(), nullable=False),
        sa.Column("embedding", Vector(), nullable=True),
        sa.UniqueConstraint("source_revision_id", "ordinal"),
    )
    op.create_index("ix_source_spans_source_revision_id", "source_spans", ["source_revision_id"])
    op.execute(
        "CREATE INDEX source_spans_bm25_idx ON source_spans "
        "USING bm25 (id, heading, content) WITH (key_field='id')"
    )
    op.add_column("sources", sa.Column("current_revision_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_sources_current_revision",
        "sources",
        "source_revisions",
        ["current_revision_id"],
        ["id"],
    )
    op.create_table(
        "drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_by_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("base_commit", sa.String(64), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("status", draft_status, nullable=False),
        sa.Column("published_commit", sa.String(64), nullable=True),
        *timestamps(),
    )
    op.create_index("ix_drafts_created_by_id", "drafts", ["created_by_id"])
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_by_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        *timestamps(),
    )
    op.create_index("ix_agent_runs_created_by_id", "agent_runs", ["created_by_id"])
    op.create_table(
        "agent_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(36),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_events_run_id", "agent_events", ["run_id"])
    op.create_table(
        "query_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("query", sa.String(1000), nullable=False),
        sa.Column("layer", sa.String(32), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("hit_count", sa.Integer(), nullable=False),
        sa.Column("channels", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_query_logs_user_id", "query_logs", ["user_id"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_table(
        "wiki_documents",
        sa.Column("path", sa.String(1024), primary_key=True),
        sa.Column("page_id", sa.String(128), nullable=False, unique=True),
        sa.Column("page_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_wiki_documents_page_id", "wiki_documents", ["page_id"])
    op.create_index("ix_wiki_documents_page_type", "wiki_documents", ["page_type"])
    op.create_index("ix_wiki_documents_commit_sha", "wiki_documents", ["commit_sha"])
    op.execute(
        "CREATE INDEX wiki_documents_bm25_idx ON wiki_documents "
        "USING bm25 (path, title, content) WITH (key_field='path')"
    )
    op.create_table(
        "knowledge_nodes",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("path", sa.String(1024), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
    )
    op.create_index("ix_knowledge_nodes_kind", "knowledge_nodes", ["kind"])
    op.create_table(
        "knowledge_edges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "source_node_id",
            sa.String(128),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_node_id",
            sa.String(128),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.UniqueConstraint("source_node_id", "target_node_id", "kind"),
    )
    op.create_index("ix_knowledge_edges_source_node_id", "knowledge_edges", ["source_node_id"])
    op.create_index("ix_knowledge_edges_target_node_id", "knowledge_edges", ["target_node_id"])
    op.create_table(
        "wiki_citations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "page_id",
            sa.String(128),
            sa.ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("citation_key", sa.String(128), nullable=False),
        sa.Column("source_revision_id", sa.String(128), nullable=False),
        sa.Column("span_id", sa.String(128), nullable=False),
        sa.Column("label", sa.String(256), nullable=True),
    )
    op.create_index(
        "ix_wiki_citations_source_revision_id",
        "wiki_citations",
        ["source_revision_id"],
    )
    op.create_table(
        "wiki_index_state",
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("wiki_index_state")
    op.drop_table("wiki_citations")
    op.drop_table("knowledge_edges")
    op.drop_table("knowledge_nodes")
    op.drop_index("wiki_documents_bm25_idx", table_name="wiki_documents")
    op.drop_table("wiki_documents")
    op.drop_table("audit_logs")
    op.drop_table("query_logs")
    op.drop_table("agent_events")
    op.drop_table("agent_runs")
    op.drop_table("drafts")
    op.drop_constraint("fk_sources_current_revision", "sources", type_="foreignkey")
    op.drop_column("sources", "current_revision_id")
    op.drop_index("source_spans_bm25_idx", table_name="source_spans")
    op.drop_table("source_spans")
    op.drop_table("source_revisions")
    op.drop_table("sources")
    op.drop_table("model_assignments")
    op.drop_table("model_profiles")
    op.drop_table("feature_flags")
    op.drop_table("auth_sessions")
    op.drop_table("users")
    postgresql.ENUM(name="draftstatus").drop(op.get_bind())
    postgresql.ENUM(name="sourcekind").drop(op.get_bind())
    postgresql.ENUM(name="userrole").drop(op.get_bind())
    postgresql.ENUM(name="modelkind").drop(op.get_bind())
