from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent import tasks
from wiki_agent.db import Base
from wiki_agent.models import (
    AgentRun,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    UserRole,
)


class FakeEmbeddings:
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(index), 1.0] for index, _ in enumerate(texts)]


class FakeModelFactory:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def embeddings(self, profile_id: str) -> FakeEmbeddings:
        assert profile_id == "embedding-profile"
        return FakeEmbeddings()


def add_source_span(
    db: Session,
    *,
    user: User,
    wiki: PersonalWiki,
    suffix: str,
) -> SourceSpan:
    source = Source(
        wiki_id=wiki.id,
        name=f"{suffix}.txt",
        kind=SourceKind.TEXT,
        created_by_id=user.id,
    )
    db.add(source)
    db.flush()
    revision = SourceRevision(
        id=f"revision-{suffix}",
        source_id=source.id,
        sha256=suffix * 64,
        object_path=f"sources/{suffix}/object",
        media_type="text/plain",
        size_bytes=10,
    )
    db.add(revision)
    db.flush()
    span = SourceSpan(
        id=f"{revision.id}:0",
        source_revision_id=revision.id,
        ordinal=0,
        content=f"content {suffix}",
        locator={},
    )
    db.add(span)
    return span


def test_embedding_backfill_updates_only_the_selected_wiki(monkeypatch) -> None:
    monkeypatch.setattr(tasks, "ModelFactory", FakeModelFactory)
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
        selected_wiki = PersonalWiki(owner_id=user.id, name="Selected")
        other_wiki = PersonalWiki(owner_id=user.id, name="Other")
        db.add_all([selected_wiki, other_wiki])
        db.flush()
        selected_span = add_source_span(
            db,
            user=user,
            wiki=selected_wiki,
            suffix="a",
        )
        other_span = add_source_span(
            db,
            user=user,
            wiki=other_wiki,
            suffix="b",
        )
        run = AgentRun(
            wiki_id=selected_wiki.id,
            created_by_id=user.id,
            kind="backfill_source_embeddings",
            input_json={
                "embedding_profile_id": "embedding-profile",
                "batch_size": 1,
            },
        )
        db.add(run)
        db.commit()

        result = tasks._backfill_source_embeddings(db, run.id)
        db.refresh(selected_span)
        db.refresh(other_span)

        assert result["updated_span_count"] == 1
        assert selected_span.embedding is not None
        assert selected_span.embedding_profile_id == "embedding-profile"
        assert other_span.embedding is None
        assert other_span.embedding_profile_id is None


def test_embedding_backfill_does_not_hold_a_transaction_during_model_call(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        class CheckedEmbeddings(FakeEmbeddings):
            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                assert not db.in_transaction()
                return super().embed_documents(texts)

        class CheckedFactory(FakeModelFactory):
            def embeddings(self, profile_id: str) -> CheckedEmbeddings:
                db.get(AgentRun, run.id)
                return CheckedEmbeddings()

        monkeypatch.setattr(tasks, "ModelFactory", CheckedFactory)
        user = User(email="backfill@qq.com", display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Backfill")
        db.add(wiki)
        db.flush()
        add_source_span(db, user=user, wiki=wiki, suffix="c")
        run = AgentRun(
            wiki_id=wiki.id,
            created_by_id=user.id,
            kind="backfill_source_embeddings",
            input_json={"embedding_profile_id": "embedding-profile", "batch_size": 1},
        )
        db.add(run)
        db.commit()

        result = tasks._backfill_source_embeddings(db, run.id)
        assert result["updated_span_count"] == 1
