from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from wiki_agent.api import wiki as wiki_api
from wiki_agent.config import Settings
from wiki_agent.db import Base
from wiki_agent.models import (
    ModelAssignment,
    ModelKind,
    ModelProfile,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    UserRole,
    WikiDocument,
    WikiIndexState,
)


def test_index_status_reports_required_wiki_rebuild_and_raw_backfill(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        wiki_api,
        "get_settings",
        lambda: Settings(_env_file=None),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            email="owner@qq.com",
            display_name="Owner",
            password_hash="unused",
            role=UserRole.USER,
        )
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Knowledge")
        db.add(wiki)
        db.flush()
        profile = ModelProfile(
            owner_id=user.id,
            name="Embedding",
            provider="openai-compatible",
            kind=ModelKind.EMBEDDING,
            base_url="https://embedding.example/v1",
            model_name="embedding",
            api_key_ciphertext="encrypted",
        )
        db.add(profile)
        db.flush()
        db.add(
            ModelAssignment(
                user_id=user.id,
                purpose="embedding",
                model_profile_id=profile.id,
            )
        )
        db.add(
            WikiDocument(
                wiki_id=wiki.id,
                path="concepts/safety.md",
                page_id="safety",
                page_type="concept",
                title="Safety",
                content="Safety",
                commit_sha="a" * 40,
            )
        )
        db.add(
            WikiIndexState(
                wiki_id=wiki.id,
                name="published",
                commit_sha="a" * 40,
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
        db.add(
            SourceSpan(
                id=f"{revision.id}:0",
                source_revision_id=revision.id,
                ordinal=0,
                content="Safety",
                locator={},
            )
        )
        db.commit()

        result = wiki_api._wiki_index_status(db, user.id, wiki.id)

    assert result["wiki_rebuild_required"] is True
    assert result["raw_backfill_required"] is True
    assert result["wiki_vector_count"] == 0
    assert result["raw_vector_count"] == 0


def test_index_rebuild_releases_model_configuration_transaction(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(email="index@qq.com", display_name="Indexer", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.commit()

        class FakeFactory:
            def __init__(self, db: Session, settings: Settings, user_id: str) -> None:
                self.db = db

            def assigned_profile_id(self, purpose: str) -> str:
                self.db.scalar(select(User.id).limit(1))
                return "embedding-profile"

            def embeddings(self, profile_id: str) -> object:
                self.db.scalar(select(User.id).limit(1))
                return object()

        class FakeIndexer:
            def __init__(self, repo: object, wiki_id: str) -> None:
                pass

            def rebuild(self, session: Session, embeddings: object, **kwargs: object) -> str:
                assert not session.in_transaction()
                return "a" * 40

        monkeypatch.setattr(wiki_api, "ModelFactory", FakeFactory)
        monkeypatch.setattr(wiki_api, "WikiIndexer", FakeIndexer)
        monkeypatch.setattr(wiki_api, "_wiki_index_status", lambda *a: {})
        wiki_api.rebuild_current_wiki_index(user, wiki, db, None)
