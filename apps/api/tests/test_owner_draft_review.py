from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from wiki_agent.api import wiki as wiki_api
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.api.schemas import PublishRequest, RejectDraftRequest
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import Base, get_db
from wiki_agent.models import (
    AgentEvent,
    AgentRun,
    AuditLog,
    Draft,
    DraftStatus,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    UserRole,
)
from wiki_agent.sources import SourceObjectStore
from wiki_agent.wiki import WikiRepository


@pytest.mark.parametrize(
    ("publish_request", "expected_message"),
    [
        (PublishRequest(), "发布草稿：Compile a source"),
        (PublishRequest(message=""), "发布草稿：Compile a source"),
        (PublishRequest(message="   "), "发布草稿：Compile a source"),
        (PublishRequest(message="好"), "好"),
        (PublishRequest(message=" Approve change "), "Approve change"),
    ],
)
def test_only_draft_creator_can_review_and_publish_in_own_wiki(
    monkeypatch: pytest.MonkeyPatch,
    publish_request: PublishRequest,
    expected_message: str,
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, storage_root=tmp_path / "storage",
                        knowledge_root=tmp_path / "knowledge")
    with Session(engine) as db:
        owner = User(email="owner@qq.com", display_name="Owner", password_hash="unused")
        other = User(
            email="admin@qq.com", display_name="Admin",
            password_hash="unused", role=UserRole.ADMIN,
        )
        db.add_all([owner, other])
        db.flush()
        wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
        other_wiki = PersonalWiki(owner_id=other.id, name="Other Wiki")
        db.add_all([wiki, other_wiki])
        db.flush()
        draft = Draft(
            wiki_id=wiki.id,
            created_by_id=owner.id,
            title="Compile a source",
            base_commit="a" * 40,
            files={"sources/manual.md": None},
        )
        rejected = Draft(
            wiki_id=wiki.id,
            created_by_id=owner.id,
            title="Reject a source",
            base_commit="a" * 40,
            files={"sources/old.md": None},
        )
        db.add_all([draft, rejected])
        db.commit()

        assert {item.id for item in wiki_api.list_my_drafts(owner, wiki, db)} == {
            draft.id, rejected.id
        }
        assert wiki_api.list_my_drafts(other, other_wiki, db) == []
        with pytest.raises(HTTPException) as denied_publish:
            wiki_api.publish_draft(
                draft.id, PublishRequest(message="Approve change"), other, other_wiki, db, None
            )
        assert denied_publish.value.status_code == 403
        with pytest.raises(HTTPException) as denied_reject:
            wiki_api.reject_draft(
                rejected.id, RejectDraftRequest(reason="Not valid"), other, other_wiki, db, settings
            )
        assert denied_reject.value.status_code == 403

        rejected_id = rejected.id
        wiki_api.reject_draft(
            rejected_id, RejectDraftRequest(reason="Unverified statement"),
            owner, wiki, db, settings,
        )
        assert db.get(Draft, rejected_id) is None
        with pytest.raises(HTTPException) as already_reviewed:
            wiki_api.reject_draft(
                rejected_id, RejectDraftRequest(reason="Rejected twice"), owner, wiki, db, settings
            )
        assert already_reviewed.value.status_code == 404

        class FakeRepository:
            def publish(self, files: object, **kwargs: object) -> SimpleNamespace:
                assert kwargs["message"] == expected_message
                return SimpleNamespace(commit="b" * 40, changed_paths=["sources/manual.md"])

        class FakeFactory:
            def __init__(self, *args: object) -> None:
                pass

            def assigned_profile_id(self, purpose: str) -> None:
                return None

        class FakeIndexer:
            def __init__(self, repository: object, wiki_id: str) -> None:
                pass

            def rebuild(self, session: Session, *args: object, **kwargs: object) -> None:
                pass

        monkeypatch.setattr(wiki_api, "ModelFactory", FakeFactory)
        monkeypatch.setattr(wiki_api, "WikiIndexer", FakeIndexer)
        published = wiki_api.publish_draft(
            draft.id, publish_request, owner, wiki, db, FakeRepository()
        )
        assert published.status is DraftStatus.PUBLISHED
        assert published.published_commit == "b" * 40


def test_review_requests_keep_length_limits_and_required_rejection_reason() -> None:
    with pytest.raises(ValidationError):
        PublishRequest(message="a" * 257)
    for reason in ["", "ab"]:
        with pytest.raises(ValidationError):
            RejectDraftRequest(reason=reason)


def add_import(
    db: Session, settings: Settings, user: User, wiki: PersonalWiki, name: str, content: bytes,
) -> tuple[Source, SourceRevision, Path]:
    obj = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes).put(
        BytesIO(content)
    )
    source = Source(
        wiki_id=wiki.id, created_by_id=user.id, name=name, kind=SourceKind.TEXT,
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
    db.add(SourceSpan(
        id=f"{revision.id}:0", source_revision_id=revision.id,
        ordinal=0, content=content.decode(), locator={},
    ))
    return source, revision, obj.path


def sample_page(body: str = "Sample text", extra_metadata: str = "") -> str:
    return (
        "---\nid: sample-page\ntype: entity\ntitle: Sample\n"
        "created: 2026-09-28T00:00:00Z\nupdated: 2026-09-28T00:00:00Z\n"
        f"{extra_metadata}---\n\n{body}"
    )


@pytest.mark.parametrize("shared_object", [False, True])
@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_rejection_deletes_import_processing_records_but_preserves_other_documents(
    tmp_path: Path, shared_object: bool, cleanup_failure: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    metadata = MetaData()
    checkpoints = [
        Table(name, metadata, Column("thread_id", String, primary_key=True), Column("data", Text))
        for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
    ]
    metadata.create_all(engine)
    settings = Settings(_env_file=None, storage_root=tmp_path / "storage",
                        knowledge_root=tmp_path / "knowledge")
    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.flush()
        source, revision, object_path = add_import(
            db, settings, user, wiki, "rejected.txt", b"Imported text",
        )
        other_source, other_revision, other_path = add_import(
            db, settings, user, wiki, "preserved.txt",
            b"Imported text" if shared_object else b"Other text",
        )
        old_obj = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes).put(
            BytesIO(b"Previous imported text")
        )
        old_revision = SourceRevision(
            source_id=source.id, sha256=old_obj.sha256,
            object_path=str(old_obj.path.relative_to(settings.storage_root.resolve())),
            media_type="text/plain", size_bytes=old_obj.size_bytes,
        )
        db.add(old_revision)
        db.flush()
        db.add(SourceSpan(
            id=f"{old_revision.id}:0", source_revision_id=old_revision.id,
            ordinal=0, content="Previous imported text", locator={},
        ))
        repository = WikiRepository(settings.wiki_library_root(wiki.id))
        repository.initialize()
        repository.publish(
            {"entities/existing.md": sample_page("Published knowledge")},
            base_commit=repository.head(), author_name="Owner",
            author_email="owner@localhost", message="Existing page",
        )
        head = repository.head()
        draft = Draft(
            wiki_id=wiki.id, created_by_id=user.id, base_commit=head, title="Rejected compilation",
            files={
                "entities/existing.md": sample_page(
                    "Replacement text cites another document.[^other]",
                    "citations:\n  other:\n"
                    f"    source_revision_id: {other_revision.id}\n"
                    f"    span_id: {other_revision.id}:0\n",
                ),
                "entities/new.md": sample_page(
                    extra_metadata=f"sources:\n  - sources/{revision.id}.md\n",
                ),
            },
        )
        other_draft = Draft(
            wiki_id=wiki.id, created_by_id=user.id, base_commit=head, title="Other draft",
            files={"entities/other.md": sample_page()},
        )
        db.add_all([draft, other_draft])
        db.flush()
        runs = [
            AgentRun(
                wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
                status="awaiting_review", input_json={"source_revision_id": revision.id},
                result_json={"draft_id": draft.id},
            ),
            AgentRun(
                wiki_id=wiki.id, created_by_id=user.id, kind="parse_source",
                status="completed", input_json={"source_revision_id": old_revision.id},
            ),
            AgentRun(
                wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
                status="failed", input_json={"source_revision_id": other_revision.id},
            ),
        ]
        db.add_all(runs)
        db.flush()
        for run in runs:
            db.add(AgentEvent(run_id=run.id, kind="draft.ready", payload={}))
            for checkpoint in checkpoints:
                db.execute(checkpoint.insert().values(
                    thread_id=run.id, data="Stored processing text",
                ))
        db.commit()
        draft_id, source_id = draft.id, source.id
        revision_ids = [revision.id, old_revision.id]
        removed_run_ids, preserved_run_id = [run.id for run in runs[:2]], runs[2].id
        app = FastAPI()
        app.include_router(wiki_api.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_wiki] = lambda: wiki
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_settings] = lambda: settings
        if cleanup_failure:
            original_unlink = Path.unlink

            def fail_old_object_cleanup(path: Path, missing_ok: bool = False) -> None:
                if path == old_obj.path:
                    raise PermissionError("Simulated original-file cleanup failure")
                original_unlink(path, missing_ok=missing_ok)

            monkeypatch.setattr(Path, "unlink", fail_old_object_cleanup)
        with TestClient(app) as client:
            response = client.post(
                f"/drafts/{draft_id}/reject", json={"reason": "Source not needed"},
            )
        if cleanup_failure:
            assert response.status_code == 500
            assert response.json()["detail"] == "Source deleted, but object cleanup failed"
        else:
            assert response.status_code == 204
            assert response.content == b""
        assert db.get(Draft, draft_id) is None
        assert db.get(Source, source_id) is None
        for revision_id in revision_ids:
            assert db.get(SourceRevision, revision_id) is None
            assert db.get(SourceSpan, f"{revision_id}:0") is None
        for run_id in removed_run_ids:
            assert db.get(AgentRun, run_id) is None
        assert db.scalar(select(AgentEvent.id).where(
            AgentEvent.run_id.in_(removed_run_ids)
        )) is None
        for checkpoint in checkpoints:
            assert list(db.scalars(select(checkpoint.c.thread_id))) == [preserved_run_id]
        assert db.get(AgentRun, preserved_run_id) is not None
        assert db.get(Source, other_source.id) is not None
        assert db.get(SourceSpan, f"{other_revision.id}:0") is not None
        assert db.get(Draft, other_draft.id) is not None
        if not cleanup_failure:
            assert object_path.exists() is shared_object
        elif shared_object:
            assert object_path.exists()
        assert other_path.exists()
        assert old_obj.path.exists() is cleanup_failure
        assert repository.head() == head
        assert repository.read_page("entities/existing.md").body.strip() == "Published knowledge"
        assert "entities/new.md" not in repository.list_pages()
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "draft.rejected"))
        assert audit is not None
        assert audit.details["deleted_sources"] == 1
        assert audit.details["title"] == "Rejected compilation"


@pytest.mark.parametrize("blocker", [
    "published", "draft", "running", "invalid_path", "wrong_wiki", "missing_revision",
])
def test_rejection_is_blocked_without_deleting_anything_when_source_cannot_be_removed(
    tmp_path: Path, blocker: str,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, storage_root=tmp_path / "storage",
                        knowledge_root=tmp_path / "knowledge")
    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.flush()
        source, revision, object_path = add_import(
            db, settings, user, wiki, "rejected.txt", b"Imported text",
        )
        repository = WikiRepository(settings.wiki_library_root(wiki.id))
        repository.initialize()
        content = sample_page(extra_metadata=f"sources:\n  - sources/{revision.id}.md\n")
        draft = Draft(
            wiki_id=wiki.id, created_by_id=user.id, base_commit=repository.head(),
            files={"entities/new.md": content}, title="Rejected compilation",
        )
        db.add(draft)
        db.flush()
        run = AgentRun(
            wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
            status="awaiting_review", input_json={"source_revision_id": revision.id},
            result_json={"draft_id": draft.id},
        )
        db.add(run)
        if blocker == "published":
            repository.publish(
                {"entities/published.md": sample_page(
                    "Published knowledge.[^claim]",
                    "citations:\n  claim:\n"
                    f"    source_revision_id: {revision.id}\n"
                    f"    span_id: {revision.id}:0\n",
                )},
                base_commit=repository.head(),
                author_name="Owner", author_email="owner@localhost", message="Published",
            )
        elif blocker == "draft":
            db.add(Draft(
                wiki_id=wiki.id, created_by_id=user.id, base_commit=repository.head(),
                files={"entities/other.md": content}, title="Other draft",
            ))
        elif blocker == "running":
            run.status = "running"
        elif blocker == "invalid_path":
            revision.object_path = "../../outside.txt"
        elif blocker == "wrong_wiki":
            other_wiki = PersonalWiki(owner_id=user.id, name="Other Wiki")
            db.add(other_wiki)
            db.flush()
            source.wiki_id = other_wiki.id
        else:
            run.input_json = {"source_revision_id": "missing-revision"}
        db.commit()
        draft_id, source_id, run_id = draft.id, source.id, run.id
        with pytest.raises(HTTPException) as failure:
            wiki_api.reject_draft(
                draft_id, RejectDraftRequest(reason="Not needed"), user, wiki, db, settings,
            )
        assert failure.value.status_code == (500 if blocker == "invalid_path" else 409)
        db.commit()
        kept_draft = db.get(Draft, draft_id)
        assert kept_draft is not None
        assert kept_draft.status is DraftStatus.OPEN
        assert db.get(Source, source_id) is not None
        assert db.get(AgentRun, run_id) is not None
        assert object_path.exists()
        assert db.scalar(select(AuditLog.id).where(AuditLog.action == "draft.rejected")) is None


@pytest.mark.parametrize("draft_status", [DraftStatus.PUBLISHED, DraftStatus.REJECTED])
def test_rejection_does_not_delete_already_reviewed_drafts(
    tmp_path: Path, draft_status: DraftStatus,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, storage_root=tmp_path / "storage",
                        knowledge_root=tmp_path / "knowledge")
    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.flush()
        draft = Draft(
            wiki_id=wiki.id, created_by_id=user.id, base_commit="a" * 40,
            files={}, title="Reviewed draft", status=draft_status,
        )
        db.add(draft)
        db.commit()
        with pytest.raises(HTTPException) as failure:
            wiki_api.reject_draft(
                draft.id, RejectDraftRequest(reason="Not needed"), user, wiki, db, settings,
            )
        assert failure.value.status_code == 409
        assert db.get(Draft, draft.id) is draft
