from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="WIKI_AGENT_",
        extra="ignore",
    )

    env: str = "development"
    secret_key: str = "development-only-secret-key-change-me"
    encryption_key: str | None = None
    database_url: str = "sqlite+pysqlite:///./wiki-agent.db"
    redis_url: str = "redis://localhost:6379/0"
    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_auth_code: SecretStr | None = None
    smtp_use_tls: bool = True
    email_from_name: str = "智库 Wiki"
    email_code_ttl_seconds: int = 600
    email_code_cooldown_seconds: int = 60
    email_code_max_attempts: int = 5
    initial_admin_username: str = "admin"
    initial_admin_password: SecretStr = SecretStr("WikiAdmin123!")
    captcha_challenge_ttl_seconds: int = 120
    captcha_token_ttl_seconds: int = 300
    captcha_min_duration_ms: int = 200
    captcha_position_tolerance_px: int = 7
    knowledge_root: Path = Path("knowledge")
    storage_root: Path = Path("storage")
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:3001"]
    )
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    max_upload_bytes: int = 50 * 1024 * 1024
    raw_rag_enabled: bool = True
    auto_create_schema: bool = True
    qa_history_context_byte_budget: int = Field(default=6144, ge=4096, le=32768)
    compilation_model_timeout_seconds: int = Field(default=180, ge=1, le=600)
    browser_service_url: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        if len(value) < 32:
            raise ValueError("WIKI_AGENT_SECRET_KEY must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_environment_security(self) -> "Settings":
        if not self.initial_admin_username.strip():
            raise ValueError("WIKI_AGENT_INITIAL_ADMIN_USERNAME must not be empty")
        if len(self.initial_admin_password.get_secret_value()) < 12:
            raise ValueError(
                "WIKI_AGENT_INITIAL_ADMIN_PASSWORD must contain at least 12 characters"
            )
        smtp_values = (self.smtp_host, self.smtp_username, self.smtp_auth_code)
        if any(value is not None for value in smtp_values) and not all(
            value is not None for value in smtp_values
        ):
            raise ValueError(
                "WIKI_AGENT_SMTP_HOST, WIKI_AGENT_SMTP_USERNAME, and "
                "WIKI_AGENT_SMTP_AUTH_CODE must be configured together"
            )
        if self.env != "development":
            if self.secret_key == "development-only-secret-key-change-me":
                raise ValueError("production requires a unique WIKI_AGENT_SECRET_KEY")
            if not self.encryption_key:
                raise ValueError("production requires WIKI_AGENT_ENCRYPTION_KEY")
            if not all(value is not None for value in smtp_values):
                raise ValueError("production requires QQ SMTP configuration")
        return self

    @property
    def wiki_root(self) -> Path:
        return self.knowledge_root / "wiki"

    @property
    def wikis_root(self) -> Path:
        return self.knowledge_root / "wikis"

    def wiki_library_root(self, _wiki_id: str) -> Path:
        return self.knowledge_root

    def legacy_wiki_library_root(self, wiki_id: str) -> Path:
        return self.wikis_root / wiki_id

    @property
    def schema_root(self) -> Path:
        return self.knowledge_root / "schema"

    @property
    def source_objects_root(self) -> Path:
        return self.storage_root / "sources"


@lru_cache
def get_settings() -> Settings:
    return Settings()
