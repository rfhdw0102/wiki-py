import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
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
    username: Mapped[str | None] = mapped_column(String(320), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    display_name: Mapped[str] = mapped_column(String(120))
    avatar_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def avatar_url(self) -> str | None:
        return "/auth/me/avatar" if self.avatar_path else None

    @property
    def local_commit_email(self) -> str:
        return self.email or f"{self.id}@localhost"


class AuthSession(Base, TimestampMixin):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
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
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(64), default="custom")
    kind: Mapped[ModelKind] = mapped_column(Enum(ModelKind))
    base_url: Mapped[str] = mapped_column(String(1024))
    model_name: Mapped[str] = mapped_column(String(256))
    api_key_ciphertext: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class ModelAssignment(Base, TimestampMixin):
    __tablename__ = "model_assignments"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    purpose: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_profile_id: Mapped[str] = mapped_column(
        ForeignKey("model_profiles.id", ondelete="CASCADE")
    )


class PersonalWiki(Base, TimestampMixin):
    __tablename__ = "wikis"
    __table_args__ = (UniqueConstraint("owner_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(500), default="")


class Source(Base, TimestampMixin):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(512))
    kind: Mapped[SourceKind] = mapped_column(Enum(SourceKind))
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    trust_level: Mapped[int] = mapped_column(default=2)
    current_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_revisions.id", use_alter=True, ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])


class SourceRevision(Base, TimestampMixin):
    __tablename__ = "source_revisions"
    __table_args__ = (UniqueConstraint("source_id", "sha256"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
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
    embedding_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Draft(Base, TimestampMixin):
    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
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
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued")
    input_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def source_revision_ids(self) -> list[str]:
        data = self.input_json or {}
        batch = data.get("source_revision_ids")
        if batch is not None:
            if not isinstance(batch, list) or not batch or any(
                not isinstance(value, str) or not value for value in batch
            ):
                raise ValueError("Invalid compilation source revision list")
            return list(dict.fromkeys(batch))
        revision = data.get("source_revision_id")
        return [revision] if isinstance(revision, str) and revision else []


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class QueryLog(Base):
    __tablename__ = "query_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    query: Mapped[str] = mapped_column(String(1000))
    layer: Mapped[str] = mapped_column(String(32))
    fallback_used: Mapped[bool] = mapped_column(Boolean)
    hit_count: Mapped[int]
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    title_is_custom: Mapped[bool] = mapped_column(Boolean, default=False)
    topic_memory: Mapped[str] = mapped_column(Text, default="")
    active_entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    memory_cursor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[str]] = mapped_column(JSON, default=list)
    answer_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class QuestionRun(Base, TimestampMixin):
    __tablename__ = "question_runs"
    __table_args__ = (
        UniqueConstraint("conversation_id", "request_id"),
        UniqueConstraint("user_id", "wiki_id", "request_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    request_id: Mapped[str] = mapped_column(String(64))
    lease_token: Mapped[str] = mapped_column(String(36), default=new_id)
    model_profile_id: Mapped[str] = mapped_column(String(36))
    question: Mapped[str] = mapped_column(String(1000))
    options_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    standalone_question: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    active_entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="running")
    stage: Mapped[str] = mapped_column(String(24), default="resolving")
    evidence_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    tool_trace: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    checkpoint_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    answer_json: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class ApiErrorLog(Base):
    __tablename__ = "api_error_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(1024))
    error_type: Mapped[str] = mapped_column(String(256))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class WikiDocument(Base):
    __tablename__ = "wiki_documents"
    __table_args__ = (
        UniqueConstraint("wiki_id", "path"),
        UniqueConstraint("wiki_id", "page_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    page_id: Mapped[str] = mapped_column(String(128), index=True)
    page_type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)
    commit_sha: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(), nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class KnowledgeNode(Base):
    __tablename__ = "knowledge_nodes"
    __table_args__ = (
        UniqueConstraint("wiki_id", "id"),
        UniqueConstraint("wiki_id", "path"),
    )

    record_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    id: Mapped[str] = mapped_column(String(128), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))


class KnowledgeEdge(Base):
    __tablename__ = "knowledge_edges"
    __table_args__ = (UniqueConstraint("wiki_id", "source_node_id", "target_node_id", "kind"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    source_node_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.record_id", ondelete="CASCADE"), index=True
    )
    target_node_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.record_id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="related")


class WikiCitation(Base):
    __tablename__ = "wiki_citations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    wiki_id: Mapped[str] = mapped_column(ForeignKey("wikis.id", ondelete="CASCADE"), index=True)
    page_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_nodes.record_id", ondelete="CASCADE")
    )
    citation_key: Mapped[str] = mapped_column(String(128))
    source_revision_id: Mapped[str] = mapped_column(String(128), index=True)
    span_id: Mapped[str] = mapped_column(String(128))
    label: Mapped[str | None] = mapped_column(String(256), nullable=True)


class WikiIndexState(Base):
    __tablename__ = "wiki_index_state"

    wiki_id: Mapped[str] = mapped_column(
        ForeignKey("wikis.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(32), primary_key=True, default="published")
    commit_sha: Mapped[str] = mapped_column(String(64))
    embedding_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
