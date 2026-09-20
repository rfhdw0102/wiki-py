import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wiki_agent.db import Base, TimestampMixin


def new_id() -> str:
    return str(uuid.uuid4())


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class SourceKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    MARKDOWN = "markdown"
    TEXT = "text"
    HTML = "html"
    WEBSITE = "website"


class DraftStatus(StrEnum):
    OPEN = "open"
    PUBLISHED = "published"
    REJECTED = "rejected"


class ModelKind(StrEnum):
    CHAT = "chat"
    EMBEDDING = "embedding"
    RERANKER = "reranker"


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthSession(Base, TimestampMixin):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FeatureFlag(Base, TimestampMixin):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class ModelProfile(Base, TimestampMixin):
    __tablename__ = "model_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[ModelKind] = mapped_column(Enum(ModelKind))
    base_url: Mapped[str] = mapped_column(String(1024))
    model_name: Mapped[str] = mapped_column(String(256))
    api_key_ciphertext: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class ModelAssignment(Base, TimestampMixin):
    __tablename__ = "model_assignments"

    purpose: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_profile_id: Mapped[str] = mapped_column(
        ForeignKey("model_profiles.id", ondelete="CASCADE")
    )


class Source(Base, TimestampMixin):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(512))
    kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind))
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trust_level: Mapped[int] = mapped_column(default=1)
    current_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_revisions.id", use_alter=True), nullable=True
    )
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])


class SourceRevision(Base, TimestampMixin):
    __tablename__ = "source_revisions"
    __table_args__ = (UniqueConstraint("source_id", "sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    object_path: Mapped[str] = mapped_column(String(1024))
    media_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int]
    status: Mapped[str] = mapped_column(String(32), default="stored")
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    source: Mapped[Source] = relationship(foreign_keys=[source_id])


class SourceSpan(Base):
    __tablename__ = "source_spans"
    __table_args__ = (UniqueConstraint("source_revision_id", "ordinal"),)

    id: Mapped[str] = mapped_column(String(192), primary_key=True)
    source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("source_revisions.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int]
    heading: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)


class Draft(Base, TimestampMixin):
    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    base_commit: Mapped[str] = mapped_column(String(64))
    files: Mapped[dict[str, str | None]] = mapped_column(JSON)
    title: Mapped[str] = mapped_column(String(256))
    status: Mapped[DraftStatus] = mapped_column(Enum(DraftStatus), default=DraftStatus.OPEN)
    published_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    input_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class QueryLog(Base):
    __tablename__ = "query_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    query: Mapped[str] = mapped_column(String(1000))
    layer: Mapped[str] = mapped_column(String(32))
    fallback_used: Mapped[bool] = mapped_column(Boolean)
    hit_count: Mapped[int]
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(128), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class WikiDocument(Base):
    __tablename__ = "wiki_documents"

    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    page_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    page_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)
    commit_sha: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class KnowledgeNode(Base):
    __tablename__ = "knowledge_nodes"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))


class KnowledgeEdge(Base):
    __tablename__ = "knowledge_edges"
    __table_args__ = (
        UniqueConstraint("source_node_id", "target_node_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_node_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), index=True
    )
    target_node_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="related")


class WikiCitation(Base):
    __tablename__ = "wiki_citations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    page_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id", ondelete="CASCADE"))
    citation_key: Mapped[str] = mapped_column(String(128))
    source_revision_id: Mapped[str] = mapped_column(String(128), index=True)
    span_id: Mapped[str] = mapped_column(String(128))
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)


class WikiIndexState(Base):
    __tablename__ = "wiki_index_state"

    name: Mapped[str] = mapped_column(String(32), primary_key=True, default="published")
    commit_sha: Mapped[str] = mapped_column(String(64))
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
