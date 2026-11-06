from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from wiki_agent.api import sources
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import Base, get_db
from wiki_agent.models import (
    AgentEvent,
    AgentRun,
    AuditLog,
    Draft,
    KnowledgeEdge,
    KnowledgeNode,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    WikiCitation,
    WikiDocument,
    WikiIndexState,
)
from wiki_agent.sources import SourceObjectStore
from wiki_agent.wiki import WikiPage, WikiRepository
from wiki_agent.wiki.deletion import remove_source_references
from wiki_agent.wiki.indexer import WikiIndexer
from wiki_agent.wiki.linking import complete_bidirectional_links
from wiki_agent.wiki.repository import WikiRepositoryError
from wiki_agent.wiki.schema import Citation, PageKind, PageMetadata


def page(
    page_id: str,
    kind: PageKind,
    body: str,
    *,
    revisions: tuple[str, ...] = (),
    citations: dict[str, str] | None = None,
    related: list[str] | None = None,
) -> str:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    return WikiPage(
        metadata=PageMetadata(
            id=page_id, type=kind, title=page_id, created=now, updated=now,
            sources=[f"sources/{revision}.md" for revision in revisions],
            citations={
                key: Citation(source_revision_id=revision, span_id=f"{revision}:0")
                for key, revision in (citations or {}).items()
            },
            related=related or [],
        ),
        body=body,
    ).to_markdown()


def add_source(
    db: Session, settings: Settings, user: User, wiki: PersonalWiki, content: bytes,
    *, source: Source | None = None,
) -> tuple[Source, SourceRevision, Path]:
    obj = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes).put(
        BytesIO(content),
    )
    if source is None:
        source = Source(
            wiki_id=wiki.id, name=content.decode(), kind=SourceKind.TEXT, created_by_id=user.id,
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
        id=f"{revision.id}:0", source_revision_id=revision.id, ordinal=0,
        content=content.decode(), locator={},
    ))
    db.flush()
    return source, revision, obj.path


def test_delete_api_cleans_all_versions_files_drafts_indexes_and_graph(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    metadata = MetaData()
    checkpoints = [
        Table(name, metadata, Column("thread_id", String, primary_key=True))
        for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
    ]
    metadata.create_all(engine)
    settings = Settings(
        _env_file=None, knowledge_root=tmp_path / "knowledge", storage_root=tmp_path / "storage",
    )
    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.flush()
        source, revision, object_path = add_source(db, settings, user, wiki, b"Removed text")
        _, old_revision, old_path = add_source(
            db, settings, user, wiki, b"Old removed text", source=source,
        )
        other, other_revision, other_path = add_source(
            db, settings, user, wiki, b"Preserved text",
        )
        source_path = f"sources/{revision.id}.md"
        other_source_path = f"sources/{other_revision.id}.md"
        shared_body = "Removed claim.[^gone]\n\nKept claim.[^kept]"
        concept_body = "Keep concept text.[^gone][^kept]"
        files = {
            source_path: page(revision.id, PageKind.SOURCE, "Removed text"),
            f"sources/{old_revision.id}.md": page(
                old_revision.id, PageKind.SOURCE, "Old removed text",
            ),
            other_source_path: page(other_revision.id, PageKind.SOURCE, "Preserved text"),
            "entities/exclusive.md": page(
                "exclusive", PageKind.ENTITY, "Exclusive claim.[^gone]",
                citations={"gone": old_revision.id},
            ),
            "entities/shared.md": page(
                "shared", PageKind.ENTITY, shared_body,
                revisions=(revision.id, other_revision.id),
                citations={"gone": revision.id, "kept": other_revision.id},
            ),
            "entities/unrelated.md": page(
                "unrelated", PageKind.ENTITY, "Independent text.",
                related=["entities/exclusive.md", source_path],
            ),
            "concepts/shared.md": page(
                "shared-concept", PageKind.CONCEPT, concept_body,
                revisions=(revision.id, other_revision.id),
                citations={"gone": revision.id, "kept": other_revision.id},
            ),
        }
        repository = WikiRepository(settings.knowledge_root)
        repository.initialize()
        repository.publish(
            complete_bidirectional_links(files, {}), base_commit=repository.head(),
            author_name="Owner", author_email="owner@localhost", message="Seed Wiki",
        )
        WikiIndexer(repository, wiki.id).rebuild(db)
        state = db.get(WikiIndexState, (wiki.id, "published"))
        assert state is not None
        state.embedding_profile_id = "existing-profile"
        for document in db.scalars(select(WikiDocument)):
            document.embedding = [0.25, 0.5]
        exclusive_draft = Draft(
            wiki_id=wiki.id, created_by_id=user.id, base_commit=repository.head(),
            title="Source draft", files={source_path: files[source_path]},
        )
        shared_draft = Draft(
            wiki_id=wiki.id, created_by_id=user.id, base_commit=repository.head(),
            title="Shared draft", files={"entities/shared-draft.md": page(
                "shared-draft", PageKind.ENTITY, shared_body,
                citations={"gone": revision.id, "kept": other_revision.id},
            )},
        )
        unrelated_draft = Draft(
            wiki_id=wiki.id, created_by_id=user.id, base_commit=repository.head(),
            title="Unrelated", files={"entities/absent.md": None},
        )
        db.add_all([exclusive_draft, shared_draft, unrelated_draft])
        db.flush()
        run = AgentRun(
            wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
            status="awaiting_review", input_json={"source_revision_id": revision.id},
            result_json={"draft_id": exclusive_draft.id},
        )
        db.add(run)
        db.flush()
        db.add(AgentEvent(run_id=run.id, kind="run.completed", payload={}))
        for checkpoint in checkpoints:
            db.execute(checkpoint.insert().values(thread_id=run.id))
        db.commit()
        source_id, draft_id, run_id = source.id, exclusive_draft.id, run.id
        deleted_revision_ids = [revision.id, old_revision.id]
        app = FastAPI()
        app.include_router(sources.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_wiki] = lambda: wiki
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as client:
            response = client.delete(f"/sources/{source_id}")
        assert response.status_code == 204, response.text
        assert db.get(Source, source_id) is None
        assert db.get(Draft, draft_id) is None
        assert db.get(AgentRun, run_id) is None
        assert db.scalar(select(AgentEvent.id)) is None
        for revision_id in deleted_revision_ids:
            assert db.get(SourceRevision, revision_id) is None
            assert db.get(SourceSpan, f"{revision_id}:0") is None
        for checkpoint in checkpoints:
            assert list(db.scalars(select(checkpoint.c.thread_id))) == []
        assert not object_path.exists()
        assert not old_path.exists()
        assert other_path.exists()
        assert db.get(Source, other.id) is not None
        assert source_path not in repository.list_pages()
        assert f"sources/{old_revision.id}.md" not in repository.list_pages()
        assert "entities/exclusive.md" not in repository.list_pages()
        shared = repository.read_page("entities/shared.md")
        assert shared.body == "Kept claim.[^kept]"
        assert shared.metadata.sources == [other_source_path]
        assert set(shared.metadata.citations) == {"kept"}
        concept = repository.read_page("concepts/shared.md")
        assert concept.body == "Keep concept text.[^kept]"
        assert concept.metadata.type is PageKind.CONCEPT
        assert concept.metadata.sources == [other_source_path]
        unrelated = repository.read_page("entities/unrelated.md")
        assert unrelated.body == "Independent text."
        assert unrelated.metadata.related == []
        db.refresh(shared_draft)
        draft_page = WikiPage.from_markdown(shared_draft.files["entities/shared-draft.md"])
        assert draft_page.body == "Kept claim.[^kept]"
        assert shared_draft.base_commit == repository.head()
        assert db.get(Draft, unrelated_draft.id).files == {"entities/absent.md": None}
        assert db.scalar(select(WikiCitation.id).where(
            WikiCitation.source_revision_id.in_(deleted_revision_ids),
        )) is None
        nodes = list(db.scalars(select(KnowledgeNode)))
        node_ids = {node.record_id for node in nodes}
        assert {node.path for node in nodes} == set(repository.list_pages())
        for edge in db.scalars(select(KnowledgeEdge)):
            assert edge.source_node_id in node_ids and edge.target_node_id in node_ids
        documents = {item.path: item for item in db.scalars(select(WikiDocument))}
        assert set(documents) == set(repository.list_pages())
        assert list(documents["entities/unrelated.md"].embedding) == [0.25, 0.5]
        assert documents["entities/shared.md"].embedding is None
        assert all(item.commit_sha == repository.head() for item in documents.values())
        assert db.get(WikiIndexState, (wiki.id, "published")).embedding_profile_id == (
            "existing-profile"
        )
        assert db.scalar(select(AuditLog.id).where(AuditLog.action == "source.deleted"))

        sources.delete_source(other.id, user, wiki, db, settings)
        assert "concepts/shared.md" not in repository.list_pages()
        assert not other_path.exists()


@pytest.mark.parametrize("failure_stage", ["publication", "index"])
def test_delete_failure_preserves_records_and_original_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None, knowledge_root=tmp_path / "knowledge", storage_root=tmp_path / "storage",
    )
    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.flush()
        source, revision, object_path = add_source(db, settings, user, wiki, b"Text")
        repository = WikiRepository(settings.knowledge_root)
        repository.initialize()
        repository.publish(
            {f"sources/{revision.id}.md": page(revision.id, PageKind.SOURCE, "Text")},
            base_commit=repository.head(), author_name="Owner",
            author_email="owner@localhost", message="Seed Wiki",
        )
        WikiIndexer(repository, wiki.id).rebuild(db)
        db.commit()
        head, source_id, revision_id = repository.head(), source.id, revision.id

        def fail_publish(*args: object, **kwargs: object) -> None:
            raise WikiRepositoryError("Simulated publication failure")

        with monkeypatch.context() as patch:
            if failure_stage == "publication":
                patch.setattr(WikiRepository, "publish", fail_publish)
            else:
                patch.setattr(WikiIndexer, "rebuild", fail_publish)
            with pytest.raises(HTTPException) as failure:
                sources.delete_source(source_id, user, wiki, db, settings)
        assert failure.value.status_code == 500
        assert ("Wiki cleanup completed" in failure.value.detail) is (
            failure_stage == "index"
        )
        assert db.get(Source, source_id) is not None
        assert db.get(SourceRevision, revision_id) is not None
        assert db.get(SourceSpan, f"{revision_id}:0") is not None
        assert db.scalar(select(AuditLog.id).where(AuditLog.action == "source.deleted")) is None
        assert db.scalar(select(WikiDocument.id)) is not None
        assert (repository.head() == head) is (failure_stage == "publication")
        assert object_path.exists()
        sources.delete_source(source_id, user, wiki, db, settings)
        assert db.get(Source, source_id) is None
        assert db.scalar(select(WikiDocument.id)) is None
        assert not object_path.exists()


def test_shared_lists_remove_only_deleted_evidence_and_keep_other_supported_claims() -> None:
    content = page(
        "shared", PageKind.ENTITY,
        "- Removed.[^gone]\n- Kept.[^kept]\n- Both.[^gone][^kept]\n\n"
        "[^gone]: Removed footnote\n    Removed continuation",
        citations={"gone": "gone", "kept": "kept"},
    )
    changes = remove_source_references({"entities/shared.md": content}, {"gone"})
    result = WikiPage.from_markdown(changes["entities/shared.md"])
    assert result.body == "- Kept.[^kept]\n- Both.[^kept]"
    assert set(result.metadata.citations) == {"kept"}
