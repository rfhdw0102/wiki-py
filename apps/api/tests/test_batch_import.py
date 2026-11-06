from contextlib import nullcontext
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_owner_draft_review import add_import
from wiki_agent import compilation, tasks
from wiki_agent.api import sources
from wiki_agent.api import wiki as wiki_api
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.api.schemas import CompileBatchRequest, PublishRequest, RejectDraftRequest
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import Base, get_db
from wiki_agent.models import (
    AgentRun,
    Draft,
    DraftStatus,
    FeatureFlag,
    ModelAssignment,
    ModelKind,
    ModelProfile,
    PersonalWiki,
    Source,
    SourceRevision,
    SourceSpan,
    User,
)
from wiki_agent.wiki import WikiPage, WikiRepository
from wiki_agent.wiki.schema import Citation, PageKind, PageMetadata


def setup(db: Session, tmp_path: Path):
    settings = Settings(
        _env_file=None, knowledge_root=tmp_path / "knowledge", storage_root=tmp_path / "storage"
    )
    owner = User(display_name="Owner", password_hash="unused")
    db.add(owner)
    db.flush()
    wiki = PersonalWiki(owner_id=owner.id, name="Wiki")
    db.add(wiki)
    db.flush()
    profile = ModelProfile(
        owner_id=owner.id, name="Compiler", kind=ModelKind.CHAT,
        base_url="http://localhost:9/v1", model_name="test", api_key_ciphertext="unused",
    )
    db.add(profile)
    db.flush()
    db.add_all([
        ModelAssignment(user_id=owner.id, purpose="compilation", model_profile_id=profile.id),
        FeatureFlag(key="source_upload", enabled=True),
        FeatureFlag(key="auto_publish", enabled=True),
    ])
    db.commit()
    repo = WikiRepository(settings.wiki_library_root(wiki.id))
    repo.initialize()
    return owner, wiki, settings, repo


class FakeChain:
    def apply_async(self):
        return None


def test_batch_upload_keeps_valid_files_and_dispatches_one_manual_review_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    dispatches = []
    monkeypatch.setattr(compilation, "chain", lambda *signatures: (
        dispatches.append(signatures) or FakeChain()
    ))
    with Session(engine, expire_on_commit=False) as db:
        owner, wiki, settings, _ = setup(db, tmp_path)
    app = FastAPI()
    app.include_router(sources.router, prefix="/api/v1")

    def session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_current_wiki] = lambda: wiki
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        response = client.post("/api/v1/sources/batch", files=[
            ("files", ("first.txt", b"First evidence", "text/plain")),
            ("files", ("unsupported.exe", b"binary", "application/octet-stream")),
            ("files", ("second.md", b"# Second evidence", "text/markdown")),
            ("files", ("empty.txt", b"", "text/plain")),
        ])
    assert response.status_code == 201
    result = response.json()
    assert len(result["items"]) == 4
    accepted = [item["revision"] for item in result["items"] if item["revision"]]
    assert len(accepted) == 2
    assert all(item["compilation_run_id"] == result["compilation_run_id"] for item in accepted)
    assert all(item["error"] for item in result["items"] if not item["revision"])
    assert len(dispatches) == 1
    with Session(engine) as db:
        runs = list(db.scalars(select(AgentRun)))
        assert len(runs) == 1
        assert runs[0].source_revision_ids == [item["id"] for item in accepted]
        assert runs[0].input_json["publication_policy"] == "manual_review"
        assert runs[0].input_json["batch"] is True
        assert len(list(db.scalars(select(Source)))) == 2
        assert db.scalar(select(Draft.id)) is None
        monkeypatch.setattr(tasks, "get_settings", lambda: settings)
        prepared = tasks._prepare_compilation_source(db, runs[0].id)
        assert prepared["source_revision_ids"] == runs[0].source_revision_ids
        assert all(db.get(SourceRevision, item["id"]).status == "parsed" for item in accepted)
        assert db.get(AgentRun, runs[0].id).status == "queued"


def make_page(kind: PageKind, name: str, members: list[str], body: str) -> str:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    citations = {
        f"fact{index}": Citation(source_revision_id=member, span_id=f"{member}:0")
        for index, member in enumerate(members)
    }
    return WikiPage(
        metadata=PageMetadata(
            id=name, type=kind, title=name, created=now, updated=now,
            sources=(
                [] if kind is PageKind.SOURCE else [f"sources/{member}.md" for member in members]
            ),
            citations=citations, status="needs_review" if kind is PageKind.PENDING else "published",
        ),
        body=body + "\n" + " ".join(f"[^fact{index}]" for index in range(len(members))),
    ).to_markdown()


def compiled_batch(
    db: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail_second: bool = False,
):
    owner, wiki, settings, repo = setup(db, tmp_path)
    imports = [
        add_import(db, settings, owner, wiki, "first.txt", b"First evidence"),
        add_import(db, settings, owner, wiki, "second.txt", b"Second evidence"),
    ]
    for _, revision, _ in imports:
        revision.status = "parsed"
    revisions = [item[1].id for item in imports]
    run = AgentRun(
        wiki_id=wiki.id, created_by_id=owner.id, kind="compile_source",
        input_json={
            "source_revision_id": revisions[0], "source_revision_ids": revisions,
            "batch": True, "model_profile_id": "model", "publication_policy": "manual_review",
        },
    )
    db.add(run)
    db.commit()
    calls = []

    class FakeFactory:
        def __init__(self, *args):
            pass

        def chat(self, *args, **kwargs):
            return object()

        def assigned_profile_id(self, purpose):
            return None

    class FakeCompiler:
        def __init__(self, model):
            pass

        def compile(self, **options):
            index = revisions.index(options["source_revision_id"])
            if fail_second and index:
                raise RuntimeError("Second source model failed")
            members = revisions[:index + 1]
            calls.append(options)
            if index:
                assert f"sources/{revisions[0]}.md" in options["existing_pages"]
                assert "First evidence" in options["existing_pages"]["entities/shared.md"]
            assert not db.in_transaction()
            return SimpleNamespace(
                files=[
                    SimpleNamespace(path=f"sources/{members[-1]}.md", content=make_page(
                        PageKind.SOURCE, members[-1], [members[-1]], "Original summary"
                    )),
                    SimpleNamespace(path="entities/shared.md", content=make_page(
                        PageKind.ENTITY, "shared", members,
                        "First evidence" + ("\nSecond evidence" if index else "")
                    )),
                    SimpleNamespace(path=f"pending/conflict-{index}.md", content=make_page(
                        PageKind.PENDING, f"conflict-{index}", members, "Unresolved difference"
                    )),
                ],
                summary="Compiled", conflicts=[f"conflict-{index}"],
            )

    class FakeVerifier:
        def __init__(self, model):
            pass

        def verify(self, files, schema):
            assert not db.in_transaction()
            assert f"sources/{revisions[0]}.md" in files
            assert f"sources/{revisions[1]}.md" in files
            assert "Second evidence" in files["entities/shared.md"]
            return SimpleNamespace(passed=True, issues=[])

    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "ModelFactory", FakeFactory)
    monkeypatch.setattr(tasks, "WikiCompiler", FakeCompiler)
    monkeypatch.setattr(tasks, "WikiVerifier", FakeVerifier)
    monkeypatch.setattr(tasks, "_compilation_checkpointer", lambda settings: nullcontext(None))
    result = tasks._compile_source(db, run.id)
    draft = db.get(Draft, result["draft_id"])
    return owner, wiki, settings, repo, imports, run, draft, calls


def test_batch_compilation_stages_shared_updates_and_produces_one_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        _, wiki, _, repo, imports, run, draft, calls = compiled_batch(db, tmp_path, monkeypatch)
        assert len(calls) == 2
        assert len(list(db.scalars(select(Draft)))) == 1
        assert draft.title == "批量导入 · 2 份资料"
        assert draft.status is DraftStatus.OPEN
        assert run.status == "awaiting_review"
        assert run.result_json["publication"] == "manual_review"
        assert run.result_json["source_revision_ids"] == [item[1].id for item in imports]
        assert "First evidence" in draft.files["entities/shared.md"]
        assert "Second evidence" in draft.files["entities/shared.md"]
        assert repo.list_pages() == []
        assert len([path for path in draft.files if path.startswith("pending/")]) == 2
        assert all(
            sources._revision_runs(db, wiki.id, [item[1].id])[0].id == run.id
            for item in imports
        )
        assert tasks._compile_source(db, run.id)["draft_id"] == draft.id
        assert len(list(db.scalars(select(Draft)))) == 1


def test_publish_batch_is_one_git_commit_and_conflicts_share_batch_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        owner, wiki, _, repo, _, run, draft, _ = compiled_batch(db, tmp_path, monkeypatch)
        initial = repo.head()

        class NoEmbedding:
            def __init__(self, *args):
                pass

            def assigned_profile_id(self, purpose):
                return None

        monkeypatch.setattr(wiki_api, "ModelFactory", NoEmbedding)
        published = wiki_api.publish_draft(draft.id, PublishRequest(), owner, wiki, db, repo)
        assert published.status is DraftStatus.PUBLISHED
        assert repo.head() != initial
        assert published.published_commit == repo.head()
        conflicts = wiki_api.list_conflicts(owner, repo, wiki, db)
        assert len(conflicts) == 2
        assert {item["batch_id"] for item in conflicts} == {run.id}
        assert {item["batch_title"] for item in conflicts} == {draft.title}
        assert all(path in repo.list_pages() for path in draft.files)


def test_reject_batch_deletes_all_member_sources_and_no_unrelated_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        owner, wiki, settings, _, imports, run, draft, _ = compiled_batch(db, tmp_path, monkeypatch)
        preserved, revision, preserved_path = add_import(
            db, settings, owner, wiki, "preserved.txt", b"Preserve unrelated bytes"
        )
        db.commit()
        run_id, draft_id = run.id, draft.id
        removed_ids = [item[0].id for item in imports]
        wiki_api.reject_draft(
            draft_id, RejectDraftRequest(reason="Reject entire batch"), owner, wiki, db, settings
        )
        assert db.get(Draft, draft_id) is None
        assert db.get(AgentRun, run_id) is None
        assert all(db.get(Source, member) is None for member in removed_ids)
        assert all(not path.exists() for _, _, path in imports)
        assert db.get(Source, preserved.id) is not None
        assert db.get(SourceRevision, revision.id) is not None
        assert preserved_path.exists()


def test_deleting_one_batch_member_preserves_other_run_and_draft_associations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        owner, wiki, settings, _, imports, run, draft, _ = compiled_batch(db, tmp_path, monkeypatch)
        sources.delete_source(imports[0][0].id, owner, wiki, db, settings)
        db.refresh(run)
        db.refresh(draft)
        remaining = imports[1][1].id
        assert run.source_revision_ids == [remaining]
        assert run.input_json["source_revision_id"] == remaining
        assert f"sources/{remaining}.md" in draft.files
        assert f"sources/{imports[0][1].id}.md" not in draft.files
        assert draft.title == "批量导入 · 1 份资料"
        assert sources._revision_runs(db, wiki.id, [remaining])[0].id == run.id


def test_active_batch_blocks_retry_and_deletion_for_every_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(compilation, "chain", lambda *args: FakeChain())
    with Session(engine) as db:
        owner, wiki, settings, _ = setup(db, tmp_path)
        imports = [
            add_import(db, settings, owner, wiki, "one.txt", b"One"),
            add_import(db, settings, owner, wiki, "two.txt", b"Two"),
        ]
        db.commit()
        run = sources.compile_source_batch(
            CompileBatchRequest(source_ids=[item[0].id for item in imports]),
            owner, wiki, db, settings,
        )
        assert len(run.source_revision_ids) == 2
        for source, _, _ in imports:
            with pytest.raises(HTTPException) as retry:
                sources.retry_source(source.id, owner, wiki, db, settings)
            assert retry.value.status_code == 409
            with pytest.raises(HTTPException) as deletion:
                sources.delete_source(source.id, owner, wiki, db, settings)
            assert deletion.value.status_code == 409


def test_all_invalid_batch_creates_no_sources_or_compilation_run(tmp_path: Path) -> None:
    from starlette.datastructures import Headers, UploadFile

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner, wiki, settings, _ = setup(db, tmp_path)
        result = sources.upload_source_batch(owner, wiki, db, settings, [
            UploadFile(BytesIO(b"bad"), filename="bad.exe",
                       headers=Headers({"content-type": "application/octet-stream"})),
            UploadFile(BytesIO(), filename="empty.txt",
                       headers=Headers({"content-type": "text/plain"})),
        ])
        assert result.compilation_run_id is None
        assert all(item.error is not None for item in result.items)
        assert db.scalar(select(Source.id)) is None
        assert db.scalar(select(AgentRun.id)) is None
        assert db.scalar(select(SourceSpan.id)) is None


def test_batch_model_failure_leaves_all_originals_and_no_partial_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        with pytest.raises(RuntimeError, match="Second source model failed"):
            compiled_batch(db, tmp_path, monkeypatch, fail_second=True)
        assert db.scalar(select(Draft.id)) is None
        assert len(list(db.scalars(select(Source)))) == 2
        run = db.scalar(select(AgentRun))
        assert run is not None
        assert run.status == "failed"
        assert len(run.source_revision_ids) == 2


def test_failed_batch_retry_keeps_all_members_and_one_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(compilation, "chain", lambda *args: FakeChain())
    with Session(engine, expire_on_commit=False) as db:
        with pytest.raises(RuntimeError):
            compiled_batch(db, tmp_path, monkeypatch, fail_second=True)
        owner = db.scalar(select(User))
        wiki = db.scalar(select(PersonalWiki))
        old_run = db.scalar(select(AgentRun))
        assert old_run is not None
        revisions = old_run.source_revision_ids
        source = db.get(Source, db.get(SourceRevision, revisions[1]).source_id)
        settings = Settings(
            _env_file=None, knowledge_root=tmp_path / "knowledge",
            storage_root=tmp_path / "storage",
        )
        retried = sources.retry_source(source.id, owner, wiki, db, settings)
        assert retried.source_revision_ids == revisions
        assert retried.input_json["publication_policy"] == "manual_review"
        assert retried.id != old_run.id
        assert retried.status == "queued"


def test_batch_compile_admission_is_atomic_and_wiki_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(compilation, "chain", lambda *args: FakeChain())
    with Session(engine) as db:
        owner, wiki, settings, _ = setup(db, tmp_path)
        first, _, _ = add_import(db, settings, owner, wiki, "one.txt", b"One")
        other_wiki = PersonalWiki(owner_id=owner.id, name="Other")
        db.add(other_wiki)
        db.flush()
        other, _, _ = add_import(db, settings, owner, other_wiki, "secret.txt", b"Secret")
        db.commit()
        with pytest.raises(HTTPException) as denied:
            sources.compile_source_batch(
                CompileBatchRequest(source_ids=[first.id, other.id]), owner, wiki, db, settings
            )
        assert denied.value.status_code == 404
        assert db.scalar(select(AgentRun.id)) is None


def test_rejection_reference_guard_preserves_the_entire_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        owner, wiki, settings, repo, imports, run, draft, _ = compiled_batch(
            db, tmp_path, monkeypatch
        )
        revision = imports[0][1].id
        repo.publish(
            {f"sources/{revision}.md": make_page(
                PageKind.SOURCE, revision, [revision], "Previously published"
            )},
            base_commit=repo.head(), author_name="Owner", author_email="owner@localhost",
            message="Previously referenced source",
        )
        with pytest.raises(HTTPException) as denied:
            wiki_api.reject_draft(
                draft.id, RejectDraftRequest(reason="Reject all"), owner, wiki, db, settings
            )
        assert denied.value.status_code == 409
        assert db.get(Draft, draft.id) is not None
        assert db.get(AgentRun, run.id) is not None
        assert all(db.get(Source, source.id) is not None for source, _, _ in imports)
        assert all(path.exists() for _, _, path in imports)


@pytest.mark.parametrize("failure", ["missing_model", "queue"])
def test_batch_configuration_and_queue_failures_keep_all_sources_in_one_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    from starlette.datastructures import Headers, UploadFile

    class BrokenChain:
        def apply_async(self):
            raise ConnectionError("Queue is unavailable")

    monkeypatch.setattr(compilation, "chain", lambda *args: BrokenChain())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner, wiki, settings, _ = setup(db, tmp_path)
        if failure == "missing_model":
            assignment = db.get(ModelAssignment, (owner.id, "compilation"))
            db.delete(assignment)
            db.commit()
        result = sources.upload_source_batch(owner, wiki, db, settings, [
            UploadFile(BytesIO(b"One"), filename="one.txt",
                       headers=Headers({"content-type": "text/plain"})),
            UploadFile(BytesIO(b"Two"), filename="two.txt",
                       headers=Headers({"content-type": "text/plain"})),
        ])
        assert result.compilation_status == "failed"
        assert result.compilation_error
        assert all(item.revision is not None for item in result.items)
        assert len(list(db.scalars(select(Source)))) == 2
        runs = list(db.scalars(select(AgentRun)))
        assert len(runs) == 1
        assert len(runs[0].source_revision_ids) == 2
        assert all(item.revision.compilation_run_id == runs[0].id for item in result.items)
        assert db.scalar(select(Draft.id)) is None


def test_deleting_failed_batch_member_keeps_remaining_members_grouped_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(compilation, "chain", lambda *args: FakeChain())
    with Session(engine, expire_on_commit=False) as db:
        owner, wiki, settings, _ = setup(db, tmp_path)
        imports = [
            add_import(db, settings, owner, wiki, name, name.encode())
            for name in ("first.txt", "second.txt", "third.txt")
        ]
        ids = [revision.id for _, revision, _ in imports]
        run = AgentRun(
            wiki_id=wiki.id, created_by_id=owner.id, kind="compile_source", status="failed",
            input_json={
                "source_revision_id": ids[0], "source_revision_ids": ids, "batch": True,
            },
        )
        db.add(run)
        db.commit()
        sources.delete_source(imports[0][0].id, owner, wiki, db, settings)
        db.refresh(run)
        assert run.source_revision_ids == ids[1:]
        assert run.status == "failed"
        retried = sources.retry_source(imports[2][0].id, owner, wiki, db, settings)
        assert retried.source_revision_ids == ids[1:]
        assert retried.input_json["batch"] is True
