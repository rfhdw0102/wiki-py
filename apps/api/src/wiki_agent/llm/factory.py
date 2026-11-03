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
    def __init__(self, db: Session, settings: Settings, user_id: str) -> None:
        self.db = db
        self.settings = settings
        self.user_id = user_id

    def chat(self, profile_id: str, *, timeout_seconds: int = 60) -> ChatOpenAI:
        profile = self._profile(profile_id, ModelKind.CHAT)
        return ChatOpenAI(
            model=profile.model_name,
            base_url=profile.base_url,
            api_key=SecretStr(self._api_key(profile)),
            timeout=timeout_seconds,
            max_retries=2,
            temperature=0,
        )

    def embeddings(self, profile_id: str) -> OpenAIEmbeddings:
        profile = self._profile(profile_id, ModelKind.EMBEDDING)
        return OpenAIEmbeddings(
            model=profile.model_name,
            base_url=profile.base_url,
            api_key=SecretStr(self._api_key(profile)),
            timeout=60,
            max_retries=2,
        )

    def reranker(self, profile_id: str) -> HttpReranker:
        profile = self._profile(profile_id, ModelKind.RERANKER)
        return HttpReranker(
            base_url=profile.base_url,
            api_key=self._api_key(profile),
            model=profile.model_name,
            endpoint="/rerank",
            timeout_seconds=60,
        )

    def assigned_profile_id(self, purpose: str) -> str | None:
        assignment = self.db.get(ModelAssignment, (self.user_id, purpose))
        return assignment.model_profile_id if assignment is not None else None

    def _api_key(self, profile: ModelProfile) -> str:
        return SecretBox(self.settings.encryption_key).decrypt(profile.api_key_ciphertext)

    def _profile(self, profile_id: str, expected_kind: ModelKind) -> ModelProfile:
        profile = self.db.get(ModelProfile, profile_id)
        if profile is None or profile.owner_id != self.user_id:
            raise ModelConfigurationError("model profile not found")
        if not profile.enabled:
            raise ModelConfigurationError("model profile is disabled")
        if profile.kind is not expected_kind:
            raise ModelConfigurationError(
                f"expected a {expected_kind.value} profile, got {profile.kind.value}"
            )
        return profile
