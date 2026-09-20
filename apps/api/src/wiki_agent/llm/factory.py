from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr
from sqlalchemy.orm import Session

from wiki_agent.config import Settings
from wiki_agent.llm.reranker import HttpReranker
from wiki_agent.models import ModelAssignment, ModelKind, ModelProfile
from wiki_agent.security import SecretBox


class ModelConfigurationError(ValueError):
    pass


class ModelFactory:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def chat(self, profile_id: str) -> ChatOpenAI:
        profile = self._profile(profile_id, ModelKind.CHAT)
        return ChatOpenAI(
            model=profile.model_name,
            base_url=profile.base_url,
            api_key=SecretStr(self._api_key(profile)),
            timeout=_number(profile, "timeout_seconds", 60),
            max_retries=_integer(profile, "max_retries", 2),
            temperature=_number(profile, "temperature", 0),
        )

    def embeddings(self, profile_id: str) -> OpenAIEmbeddings:
        profile = self._profile(profile_id, ModelKind.EMBEDDING)
        return OpenAIEmbeddings(
            model=profile.model_name,
            base_url=profile.base_url,
            api_key=SecretStr(self._api_key(profile)),
            timeout=_number(profile, "timeout_seconds", 60),
            max_retries=_integer(profile, "max_retries", 2),
        )

    def reranker(self, profile_id: str) -> HttpReranker:
        profile = self._profile(profile_id, ModelKind.RERANKER)
        endpoint = profile.config.get("endpoint", "/rerank")
        if not isinstance(endpoint, str):
            raise ModelConfigurationError(f"{profile.name}.endpoint must be a string")
        return HttpReranker(
            base_url=profile.base_url,
            api_key=self._api_key(profile),
            model=profile.model_name,
            endpoint=endpoint,
            timeout_seconds=_number(profile, "timeout_seconds", 60),
        )

    def assigned_profile_id(self, purpose: str) -> str | None:
        assignment = self.db.get(ModelAssignment, purpose)
        return assignment.model_profile_id if assignment is not None else None

    def _api_key(self, profile: ModelProfile) -> str:
        return SecretBox(self.settings.encryption_key).decrypt(profile.api_key_ciphertext)

    def _profile(self, profile_id: str, expected_kind: ModelKind) -> ModelProfile:
        profile = self.db.get(ModelProfile, profile_id)
        if profile is None:
            raise ModelConfigurationError("model profile not found")
        if not profile.enabled:
            raise ModelConfigurationError("model profile is disabled")
        if profile.kind is not expected_kind:
            raise ModelConfigurationError(
                f"expected a {expected_kind.value} profile, got {profile.kind.value}"
            )
        return profile


def _number(profile: ModelProfile, key: str, default: float) -> float:
    value = profile.config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ModelConfigurationError(f"{profile.name}.{key} must be a number")
    return float(value)


def _integer(profile: ModelProfile, key: str, default: int) -> int:
    value = profile.config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelConfigurationError(f"{profile.name}.{key} must be an integer")
    return value
