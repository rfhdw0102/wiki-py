import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.api import models as model_api
from wiki_agent.api.schemas import (
    ModelAssignmentUpdate,
    ModelConnectionTestRequest,
    ModelProfileCreate,
    ModelProfileUpdate,
)
from wiki_agent.config import Settings
from wiki_agent.db import Base
from wiki_agent.llm import ModelFactory
from wiki_agent.llm.factory import ModelConfigurationError
from wiki_agent.models import (
    ModelAssignment,
    ModelKind,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    UserRole,
    WikiIndexState,
)
from wiki_agent.security import SecretBox, hash_password


def make_user(email: str) -> User:
    return User(
        email=email,
        display_name=email.split("@", 1)[0],
        password_hash=hash_password("long-password-1"),
        role=UserRole.USER,
    )


def test_model_profiles_and_assignments_are_isolated_per_user(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        encryption_key=Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(model_api, "get_settings", lambda: settings)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        first = make_user("first@qq.com")
        second = make_user("second@qq.com")
        db.add_all([first, second])
        db.commit()
        profile = model_api.create_model_profile(
            ModelProfileCreate(
                name="默认对话模型",
                kind=ModelKind.CHAT,
                base_url="https://llm.example/v1",
                model_name="example-chat",
                api_key="secret",
            ),
            first,
            db,
        )
        model_api.create_model_profile(
            ModelProfileCreate(
                name="默认对话模型",
                kind=ModelKind.CHAT,
                base_url="https://other.example/v1",
                model_name="other-chat",
                api_key="other-secret",
            ),
            second,
            db,
        )

        assert [item.id for item in model_api.list_model_profiles(first, db)] == [profile.id]
        assert len(model_api.list_model_profiles(second, db)) == 1
        updated = model_api.update_model_profile(
            profile.id,
            ModelProfileUpdate(
                name="主对话模型",
                base_url="https://api.deepseek.com/v1",
                model_name="deepseek-chat",
                enabled=True,
            ),
            first,
            db,
        )
        assert updated.name == "主对话模型"
        assert updated.provider == "openai-compatible"
        assert updated.model_name == "deepseek-chat"

        assignment = model_api.update_model_assignment(
            "compilation",
            ModelAssignmentUpdate(model_profile_id=profile.id),
            first,
            db,
        )
        assert assignment.user_id == first.id
        assert ModelFactory(db, settings, first.id).assigned_profile_id("compilation") == profile.id
        assert ModelFactory(db, settings, second.id).assigned_profile_id("compilation") is None

        try:
            model_api.update_model_assignment(
                "compilation",
                ModelAssignmentUpdate(model_profile_id=profile.id),
                second,
                db,
            )
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("another user's model profile must not be assignable")

        try:
            ModelFactory(db, settings, second.id).chat(profile.id)
        except ModelConfigurationError:
            pass
        else:
            raise AssertionError("another user's model credentials must not be readable")


def test_compilation_timeout_override_does_not_change_other_chat_calls(monkeypatch) -> None:
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode())
    monkeypatch.setattr(model_api, "get_settings", lambda: settings)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = make_user("timeout@qq.com")
        db.add(user)
        db.flush()
        profile = model_api.create_model_profile(
            ModelProfileCreate(
                name="Compilation model",
                kind=ModelKind.CHAT,
                base_url="https://llm.example/v1",
                model_name="example-chat",
                api_key="test-only",
            ),
            user,
            db,
        )
        factory = ModelFactory(db, settings, user.id)
        assert factory.chat(profile.id).request_timeout == 60
        assert factory.chat(
            profile.id, timeout_seconds=settings.compilation_model_timeout_seconds
        ).request_timeout == 180


def test_openai_compatible_connection_test(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        encryption_key=Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(model_api, "get_settings", lambda: settings)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    def fake_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> httpx.Response:
        assert url == "https://llm.example/v1/models"
        assert headers["Authorization"] == "Bearer secret"
        assert timeout == 5
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "example-chat"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(model_api.httpx, "get", fake_get)
    with Session(engine) as db:
        user = make_user("first@qq.com")
        db.add(user)
        db.commit()
        result = model_api.test_model_connection(
            ModelConnectionTestRequest(
                base_url="https://llm.example/v1",
                model_name="example-chat",
                api_key="secret",
            ),
            user,
            db,
        )

    assert result.healthy is True
    assert result.message == "连接成功，模型 example-chat 可用"
    assert result.latency_ms >= 1


def test_connection_test_uses_saved_key_and_reports_missing_model(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        encryption_key=Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(model_api, "get_settings", lambda: settings)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    expected_api_key: str | None = None

    def fake_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: int,
    ) -> httpx.Response:
        scheme, separator, credential = headers["Authorization"].partition(" ")
        assert scheme == "Bearer"
        assert separator
        assert expected_api_key is not None
        assert credential == expected_api_key
        return httpx.Response(
            200,
            json={"object": "list", "data": [{"id": "available-model"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(model_api.httpx, "get", fake_get)
    with Session(engine) as db:
        user = make_user("first@qq.com")
        db.add(user)
        db.commit()
        profile = model_api.create_model_profile(
            ModelProfileCreate(
                name="默认对话模型",
                kind=ModelKind.CHAT,
                base_url="https://llm.example/v1",
                model_name="configured-model",
                api_key="saved-secret",
            ),
            user,
            db,
        )
        expected_api_key = SecretBox(settings.encryption_key).decrypt(profile.api_key_ciphertext)
        result = model_api.test_model_connection(
            ModelConnectionTestRequest(
                profile_id=profile.id,
                base_url=profile.base_url,
                model_name=profile.model_name,
            ),
            user,
            db,
        )

    assert result.healthy is False
    assert result.message == "连接成功，但未找到模型 configured-model"


def test_updating_assigned_embedding_profile_invalidates_existing_vectors(
    monkeypatch,
) -> None:
    settings = Settings(
        _env_file=None,
        encryption_key=Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(model_api, "get_settings", lambda: settings)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = make_user("owner@qq.com")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Knowledge")
        db.add(wiki)
        db.flush()
        profile = model_api.create_model_profile(
            ModelProfileCreate(
                name="Embedding",
                kind=ModelKind.EMBEDDING,
                base_url="https://embedding.example/v1",
                model_name="embedding-v1",
                api_key="secret",
            ),
            user,
            db,
        )
        db.add(
            ModelAssignment(
                user_id=user.id,
                purpose="embedding",
                model_profile_id=profile.id,
            )
        )
        source = Source(
            wiki_id=wiki.id,
            name="manual.txt",
            kind=SourceKind.TEXT,
            created_by_id=user.id,
        )
        db.add(source)
        db.flush()
        revision = SourceRevision(
            source_id=source.id,
            sha256="a" * 64,
            object_path="sources/a/object",
            media_type="text/plain",
            size_bytes=10,
        )
        db.add(revision)
        db.flush()
        span = SourceSpan(
            id=f"{revision.id}:0",
            source_revision_id=revision.id,
            ordinal=0,
            content="content",
            locator={},
            embedding=[1.0, 0.0],
            embedding_profile_id=profile.id,
        )
        state = WikiIndexState(
            wiki_id=wiki.id,
            name="published",
            commit_sha="a" * 40,
            embedding_profile_id=profile.id,
        )
        db.add_all([span, state])
        db.commit()

        model_api.update_model_profile(
            profile.id,
            ModelProfileUpdate(
                name=profile.name,
                base_url=profile.base_url,
                model_name="embedding-v2",
                enabled=True,
            ),
            user,
            db,
        )
        db.refresh(span)
        db.refresh(state)

        assert span.embedding is not None
        assert span.embedding_profile_id is None
        assert state.embedding_profile_id is None
