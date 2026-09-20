from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
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
    knowledge_root: Path = Path("knowledge")
    storage_root: Path = Path("storage")
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:3001"]
    )
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    max_upload_bytes: int = 50 * 1024 * 1024
    raw_rag_enabled: bool = True
    auto_create_schema: bool = True

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
        if bool(self.bootstrap_admin_email) != bool(self.bootstrap_admin_password):
            raise ValueError(
                "bootstrap administrator email and password must be configured together"
            )
        if self.bootstrap_admin_password and len(self.bootstrap_admin_password) < 12:
            raise ValueError("bootstrap administrator password must contain at least 12 characters")
        if self.env != "development":
            if self.secret_key == "development-only-secret-key-change-me":
                raise ValueError("production requires a unique WIKI_AGENT_SECRET_KEY")
            if not self.encryption_key:
                raise ValueError("production requires WIKI_AGENT_ENCRYPTION_KEY")
        return self

    @property
    def wiki_root(self) -> Path:
        return self.knowledge_root / "wiki"

    @property
    def schema_root(self) -> Path:
        return self.knowledge_root / "schema"

    @property
    def source_objects_root(self) -> Path:
        return self.storage_root / "sources"


@lru_cache
def get_settings() -> Settings:
    return Settings()
