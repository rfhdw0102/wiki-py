from io import BytesIO
from pathlib import Path

import pytest
import wiki_agent.compilation as compilation
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from wiki_agent.api import sources
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import Base, get_db
from wiki_agent.models import (
    AgentRun,
    Draft,
    FeatureFlag,
    ModelAssignment,
    ModelKind,
    ModelProfile,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    WikiDocument,
)
from wiki_agent.sources.storage import SourceObjectStore
from wiki_agent.wiki import WikiRepository
from wiki_agent.wiki.indexer import WikiIndexer


def setup_source(
    db: Session, settings: Settings
) -> tuple[User, PersonalWiki, Source, SourceRevision, Path]:
    user = User(email="owner@qq.com", display_name="Owner", password_hash="unused")
    db.add(user)
    db.flush()
    wiki = PersonalWiki(owner_id=user.id, name="My Wiki")
    db.add(wiki)
    db.flush()
    WikiRepository(settings.wiki_library_root(wiki.id)).initialize()
    obj = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes).put(
        BytesIO(b"Sample text")
    )
    source = Source(
        wiki_id=wiki.id, name="sample.txt", kind=SourceKind.TEXT, created_by_id=user.id
    )
    db.add(source)
    db.flush()
    revision = SourceRevision(
        source_id=source.id, sha256=obj.sha256,
        object_path=str(obj.path.relative_to(settings.storage_root.resolve())),
        media_type="text/plain", size_bytes=obj.size_bytes,
    )
    db.add(revision)
    db.flush()
    source.current_revision_id = revision.id
    db.commit()
    return user, wiki, source, revision, obj.path


def test_retry_requeues_failed_revision_once_and_is_wiki_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeChain:
        def apply_async(self) -> None:
            pass

    monkeypatch.setattr(compilation, "chain", lambda *tasks: FakeChain())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge",
                        storage_root=tmp_path / "storage")
    with Session(engine) as db:
        user, wiki, source, revision, _ = setup_source(db, settings)
        other = PersonalWiki(owner_id=user.id, name="Other")
        db.add(other)
        failed = AgentRun(
            wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
            status="failed", input_json={"source_revision_id": revision.id},
            error="No compilation model is assigned",
        )
        db.add(failed)
        db.commit()
        with pytest.raises(HTTPException) as no_model:
            sources.retry_source(source.id, user, wiki, db, settings)
        assert no_model.value.status_code == 422
        profile = ModelProfile(
            owner_id=user.id, name="Compiler", provider="custom", kind=ModelKind.CHAT,
            base_url="http://localhost:1234/v1", model_name="local",
            api_key_ciphertext="not-used",
        )
        db.add(profile)
        db.flush()
        db.add(ModelAssignment(user_id=user.id, purpose="compilation", model_profile_id=profile.id))
        db.add(FeatureFlag(key="auto_publish", enabled=False))
        db.commit()

        with pytest.raises(HTTPException) as denied:
            sources.retry_source(source.id, user, other, db, settings)
        assert denied.value.status_code == 404
        retried = sources.retry_source(source.id, user, wiki, db, settings)
        assert retried.id != failed.id
        assert retried.status == "queued"
        assert retried.input_json["source_revision_id"] == revision.id
        assert retried.input_json["publication_policy"] == "manual_review"
        with pytest.raises(HTTPException) as duplicate:
            sources.retry_source(source.id, user, wiki, db, settings)
        assert duplicate.value.status_code == 409


def test_delete_failed_source_cleans_runs_spans_and_unshared_object(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge",
                        storage_root=tmp_path / "storage")
    with Session(engine) as db:
        user, wiki, source, revision, object_path = setup_source(db, settings)
        db.add(SourceSpan(
            id=f"{revision.id}:0", source_revision_id=revision.id, ordinal=0,
            content="Sample text", locator={},
        ))
        run = AgentRun(
            wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
            status="failed", input_json={"source_revision_id": revision.id},
        )
        db.add(run)
        db.commit()
        source_id, run_id = source.id, run.id
        sources.delete_source(source_id, user, wiki, db, settings)
        assert db.get(Source, source_id) is None
        assert db.get(SourceRevision, revision.id) is None
        assert db.get(SourceSpan, f"{revision.id}:0") is None
        assert db.get(AgentRun, run_id) is None
        assert not object_path.exists()


def test_original_content_requires_wiki_access_and_existing_file(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None, knowledge_root=tmp_path / "knowledge",
        storage_root=tmp_path / "storage",
    )
    with Session(engine) as db:
        user, wiki, source, revision, object_path = setup_source(db, settings)
        newer = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes).put(
            BytesIO(b"Updated text")
        )
        new_revision = SourceRevision(
            source_id=source.id,
            sha256=newer.sha256,
            object_path=str(newer.path.relative_to(settings.storage_root.resolve())),
            media_type="text/plain",
            size_bytes=newer.size_bytes,
        )
        db.add(new_revision)
        db.flush()
        source.current_revision_id = new_revision.id
        other = PersonalWiki(owner_id=user.id, name="Other")
        db.add(other)
        db.commit()
        response = sources.read_source_content(revision.id, wiki, db, settings)
        assert response.path == object_path
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert sources.get_source_revision(revision.id, wiki, db) == revision
        app = FastAPI()
        app.include_router(sources.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_wiki] = lambda: wiki
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as client:
            fetched = client.get(f"/sources/revisions/{revision.id}/content")
            assert fetched.status_code == 200
            assert fetched.content == b"Sample text"
            assert fetched.headers["content-type"] == "text/plain; charset=utf-8"

        with pytest.raises(HTTPException) as denied:
            sources.read_source_content(revision.id, other, db, settings)
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as denied_revision:
            sources.get_source_revision(revision.id, other, db)
        assert denied_revision.value.status_code == 404

        original_path = revision.object_path
        revision.object_path = "../../outside.txt"
        db.flush()
        with pytest.raises(HTTPException) as invalid_path:
            sources.read_source_content(revision.id, wiki, db, settings)
        assert invalid_path.value.status_code == 500
        revision.object_path = original_path
        db.flush()

        object_path.unlink()
        with pytest.raises(HTTPException) as missing:
            sources.read_source_content(revision.id, wiki, db, settings)
        assert missing.value.status_code == 404


def test_delete_blocks_active_runs_but_cleans_wiki_and_draft_references(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge",
                        storage_root=tmp_path / "storage")
    with Session(engine) as db:
        user, wiki, source, revision, object_path = setup_source(db, settings)
        run = AgentRun(
            wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
            status="queued", input_json={"source_revision_id": revision.id},
        )
        db.add(run)
        db.commit()
        with pytest.raises(HTTPException) as active:
            sources.delete_source(source.id, user, wiki, db, settings)
        assert active.value.status_code == 409
        run.status = "failed"
        draft = Draft(
            wiki_id=wiki.id, created_by_id=user.id, base_commit="a" * 40,
            files={f"sources/{revision.id}.md": "---\nid: sample-page\ntype: source\n"
                   "title: Sample\ncreated: 2026-09-28T00:00:00Z\n"
                   "updated: 2026-09-28T00:00:00Z\n---\n\nSample text"},
            title="Draft",
        )
        db.add(draft)
        db.commit()
        repository = WikiRepository(settings.wiki_library_root(wiki.id))
        repository.publish(
            {f"sources/{revision.id}.md":
             "---\nid: sample-page\ntype: source\ntitle: Sample\n"
             "created: 2026-09-28T00:00:00Z\nupdated: 2026-09-28T00:00:00Z\n"
             "---\n\nSample text"},
            base_commit=repository.head(), author_name="Owner",
            author_email="owner@qq.com", message="Publish summary",
        )
        with pytest.raises(HTTPException) as no_retry:
            sources.retry_source(source.id, user, wiki, db, settings)
        assert no_retry.value.status_code == 409
        source_id, draft_id = source.id, draft.id
        sources.delete_source(source_id, user, wiki, db, settings)
        assert db.get(Source, source_id) is None
        assert db.get(Draft, draft_id) is None
        assert repository.list_pages() == []
        assert not object_path.exists()


def test_delete_preserves_shared_object_and_rejects_wrong_wiki(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge",
                        storage_root=tmp_path / "storage")
    with Session(engine) as db:
        user, wiki, source, revision, object_path = setup_source(db, settings)
        second_wiki = PersonalWiki(owner_id=user.id, name="Second")
        db.add(second_wiki)
        db.flush()
        duplicate = Source(
            wiki_id=second_wiki.id, name="same.txt",
            kind=SourceKind.TEXT, created_by_id=user.id
        )
        db.add(duplicate)
        db.flush()
        db.add(SourceRevision(
            source_id=duplicate.id, sha256=revision.sha256,
            object_path=revision.object_path, media_type="text/plain", size_bytes=11,
        ))
        db.commit()
        with pytest.raises(HTTPException) as denied:
            sources.delete_source(source.id, user, second_wiki, db, settings)
        assert denied.value.status_code == 404
        sources.delete_source(source.id, user, wiki, db, settings)
        assert object_path.exists()


def test_delete_cleans_citations_even_without_source_summary_page(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None, knowledge_root=tmp_path / "knowledge",
        storage_root=tmp_path / "storage",
    )
    with Session(engine) as db:
        user, wiki, source, revision, object_path = setup_source(db, settings)
        repository = WikiRepository(settings.wiki_library_root(wiki.id))
        repository.publish(
            {
                "entities/evidence.md": (
                    "---\nid: evidence\ntype: entity\ntitle: Evidence\n"
                    "created: 2026-09-28T00:00:00Z\nupdated: 2026-09-28T00:00:00Z\n"
                    "citations:\n  claim:\n"
                    f"    source_revision_id: {revision.id}\n"
                    f"    span_id: {revision.id}:0\n"
                    "---\n\nThis claim cites a source.[^claim]"
                )
            },
            base_commit=repository.head(), author_name="Owner",
            author_email="owner@qq.com", message="Add citation",
        )
        WikiIndexer(repository, wiki.id).rebuild(db)
        assert db.scalar(select(WikiDocument.id)) is not None
        sources.delete_source(source.id, user, wiki, db, settings)
        assert repository.list_pages() == []
        assert db.scalar(select(WikiDocument.id)) is None
        assert not object_path.exists()
