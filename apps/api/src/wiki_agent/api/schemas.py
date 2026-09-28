from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field

from wiki_agent.models import DraftStatus, ModelKind, SourceKind, UserRole


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    display_name: str
    role: UserRole
    is_active: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)
    role: UserRole = UserRole.USER


class UserStatusUpdate(BaseModel):
    is_active: bool


class PasswordResetRequest(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class SourceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: SourceKind
    current_revision_id: str | None
    trust_level: int
    created_at: datetime


class SourceRevisionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_id: str
    sha256: str
    media_type: str
    size_bytes: int
    status: str
    created_at: datetime


class SourceSpanView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_revision_id: str
    ordinal: int
    heading: str | None
    content: str
    locator: dict[str, object]


class WebsiteSourceCreate(BaseModel):
    url: AnyHttpUrl


class SourceTrustUpdate(BaseModel):
    trust_level: int = Field(ge=0, le=3)


class DraftCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    base_commit: str = Field(min_length=7, max_length=64)
    files: dict[str, str | None] = Field(min_length=1)


class DraftView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    base_commit: str
    files: dict[str, str | None]
    status: DraftStatus
    published_commit: str | None
    created_by_id: str
    created_at: datetime


class PublishRequest(BaseModel):
    message: str = Field(min_length=3, max_length=256)


class RejectDraftRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class RollbackRequest(BaseModel):
    target_commit: str = Field(min_length=7, max_length=64)
    title: str = Field(min_length=1, max_length=256)


class FeatureFlagView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    enabled: bool
    config: dict[str, object]


class FeatureFlagUpdate(BaseModel):
    enabled: bool
    config: dict[str, object] = Field(default_factory=dict)


class ModelProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: ModelKind
    base_url: str = Field(pattern=r"^https?://", max_length=1024)
    model_name: str = Field(min_length=1, max_length=256)
    api_key: str = Field(min_length=1, max_length=4096)
    enabled: bool = True
    config: dict[str, object] = Field(default_factory=dict)


class ModelProfileView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: ModelKind
    base_url: str
    model_name: str
    enabled: bool
    config: dict[str, object]


class ModelAssignmentUpdate(BaseModel):
    model_profile_id: str


class ModelAssignmentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    purpose: str
    model_profile_id: str


class SchemaUpdate(BaseModel):
    base_commit: str = Field(min_length=7, max_length=64)
    content: str = Field(min_length=1, max_length=200_000)
    message: str = Field(min_length=3, max_length=256)


class WikiSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    entry_node_ids: list[str] = Field(default_factory=list, max_length=20)
    embedding_profile_id: str | None = None
    reranker_profile_id: str | None = None
    allow_raw_fallback: bool = True
    minimum_core_hits: int = Field(default=1, ge=1, le=10)
    limit: int = Field(default=10, ge=1, le=50)


class WikiQuestionRequest(WikiSearchRequest):
    chat_profile_id: str | None = None


class CompileSourceRequest(BaseModel):
    source_revision_id: str
    model_profile_id: str | None = None
    publication_policy: str = Field(pattern=r"^(manual_review|low_risk_auto_publish)$")


class ParseSourceRequest(BaseModel):
    source_revision_id: str


class AgentRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    status: str
    input_json: dict[str, object]
    result_json: dict[str, object] | None
    error: str | None
    created_at: datetime
