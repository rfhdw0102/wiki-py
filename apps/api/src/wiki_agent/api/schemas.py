from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field

from wiki_agent.auth import AuthPurpose
from wiki_agent.models import DraftStatus, ModelKind, SourceKind, UserRole


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    username: str | None
    email: EmailStr | None
    display_name: str
    avatar_url: str | None = None
    updated_at: datetime
    role: UserRole
    is_active: bool
    must_change_password: bool


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=8, max_length=256)
    captcha_token: str = Field(min_length=32, max_length=128)


class CaptchaChallengeView(BaseModel):
    challenge_id: str
    background_image: str
    puzzle_image: str
    image_width: int
    image_height: int
    puzzle_width: int
    puzzle_height: int
    puzzle_top: int
    expires_in: int


class CaptchaVerifyRequest(BaseModel):
    challenge_id: str = Field(min_length=16, max_length=128)
    x: float = Field(ge=0, le=640)
    y: float = Field(ge=-160, le=160)
    duration: int = Field(ge=1, le=120_000)
    trail: list[tuple[float, float]] = Field(min_length=2, max_length=1024)


class CaptchaVerifyResponse(BaseModel):
    captcha_token: str
    expires_in: int


class EmailCodeRequest(BaseModel):
    email: EmailStr
    purpose: AuthPurpose


class EmailBindingCodeRequest(BaseModel):
    email: EmailStr


class PasswordRecoveryRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    email_code: str = Field(pattern=r"^\d{6}$")


class ProfileUpdateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class EmailBindingRequest(BaseModel):
    email: EmailStr
    email_code: str = Field(pattern=r"^\d{6}$")


class AccountDeletionRequest(BaseModel):
    current_password: str = Field(min_length=8, max_length=256)


class MessageView(BaseModel):
    message: str


class WikiCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class WikiUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class WikiView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    created_at: datetime
    updated_at: datetime


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


class SourceIngestView(SourceRevisionView):
    compilation_run_id: str
    compilation_status: str
    compilation_error: str | None


class SourceBatchItemView(BaseModel):
    file_name: str
    revision: SourceIngestView | None
    error: str | None


class SourceBatchIngestView(BaseModel):
    items: list[SourceBatchItemView]
    compilation_run_id: str | None
    compilation_status: str | None
    compilation_error: str | None


class CompileBatchRequest(BaseModel):
    source_ids: list[str] = Field(min_length=1)


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
    message: str = Field(default="", max_length=256)


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


class ModelProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: ModelKind
    base_url: str = Field(pattern=r"^https?://", max_length=1024)
    model_name: str = Field(min_length=1, max_length=256)
    api_key: str = Field(min_length=1, max_length=4096)
    enabled: bool = True


class ModelProfileUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(pattern=r"^https?://", max_length=1024)
    model_name: str = Field(min_length=1, max_length=256)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    enabled: bool = True


class ModelConnectionTestRequest(BaseModel):
    profile_id: str | None = None
    base_url: str = Field(pattern=r"^https?://", max_length=1024)
    model_name: str = Field(min_length=1, max_length=256)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)


class ModelConnectionTestResult(BaseModel):
    healthy: bool
    message: str
    latency_ms: int


class ModelProfileView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    kind: ModelKind
    base_url: str
    model_name: str
    enabled: bool


class ModelAssignmentUpdate(BaseModel):
    model_profile_id: str


class ModelAssignmentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    purpose: str
    model_profile_id: str


class SchemaUpdate(BaseModel):
    base_commit: str = Field(min_length=7, max_length=64)
    content: str = Field(min_length=1, max_length=200_000)


class WikiSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    entry_node_ids: list[str] = Field(default_factory=list, max_length=20)
    embedding_profile_id: str | None = None
    reranker_profile_id: str | None = None
    allow_raw_fallback: bool = True
    minimum_core_hits: int = Field(default=3, ge=1, le=10)
    require_cross_channel_top: bool = True
    limit: int = Field(default=10, ge=1, le=50)


class WikiQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    chat_profile_id: str | None = None
    embedding_profile_id: str | None = None
    reranker_profile_id: str | None = None
    allow_raw_fallback: bool = True
    entry_node_ids: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=10, ge=1, le=50)
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)
    request_id: str | None = Field(default=None, min_length=1, max_length=64)


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
