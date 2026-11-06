from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from test_source_lifecycle import setup_source
from wiki_agent.api import sources
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import Base, get_db
from wiki_agent.models import AuditLog, PersonalWiki, Source, SourceKind, SourceSpan
from wiki_agent.sources.parser import DocumentParser


@pytest.fixture
def rename_context(tmp_path: Path) -> Iterator[tuple[TestClient, Session, Source, Path]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None, knowledge_root=tmp_path / "knowledge", storage_root=tmp_path / "storage",
    )
    with Session(engine) as db:
        user, wiki, source, revision, object_path = setup_source(db, settings)
        db.add(SourceSpan(
            id=f"{revision.id}:0", source_revision_id=revision.id, ordinal=0,
            content="Parsed original content", locator={"page": 1},
        ))
        db.commit()
        app = FastAPI()
        app.include_router(sources.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_wiki] = lambda: wiki
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as client:
            yield client, db, source, object_path
    engine.dispose()


def test_rename_persists_display_name_without_changing_original_or_parsing(
    rename_context: tuple[TestClient, Session, Source, Path],
) -> None:
    client, db, source, object_path = rename_context
    revision_id = source.current_revision_id
    revision_before = client.get(f"/sources/revisions/{revision_id}").json()
    parsed_before = DocumentParser().parse(object_path, source.kind)
    response = client.patch(f"/sources/{source.id}", json={"name": "  新显示名称  "})
    assert response.status_code == 200
    assert response.json()["name"] == "新显示名称"
    assert response.json()["current_revision_id"] == revision_id
    assert response.json()["kind"] == "text"
    db.expire_all()
    assert db.get(Source, source.id).name == "新显示名称"
    assert client.get("/sources").json()[0]["name"] == "新显示名称"
    assert client.get(f"/sources/revisions/{revision_id}").json() == revision_before
    assert object_path.read_bytes() == b"Sample text"
    assert DocumentParser().parse(object_path, source.kind) == parsed_before
    assert client.get(f"/sources/revisions/{revision_id}/content").content == b"Sample text"
    span = db.get(SourceSpan, f"{revision_id}:0")
    assert span.content == "Parsed original content"
    assert span.locator == {"page": 1}
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "source.renamed"))
    assert audit.resource_id == source.id
    assert audit.actor_id == source.created_by_id
    assert audit.details == {
        "old_name": "sample.txt", "name": "新显示名称", "wiki_id": source.wiki_id,
    }


def test_website_names_and_512_unicode_characters_do_not_require_extensions(
    rename_context: tuple[TestClient, Session, Source, Path],
) -> None:
    client, db, source, _ = rename_context
    source.kind = SourceKind.WEBSITE
    db.commit()
    name = "😀" * 512
    response = client.patch(f"/sources/{source.id}", json={"name": f"  {name}  "})
    assert response.status_code == 200
    assert response.json()["name"] == name
    assert response.json()["kind"] == "website"


@pytest.mark.parametrize("name", [
    "", "   ", "a" * 513, "folder/name", r"folder\name", "a\nb", "\nname",
    "name\r", "a\vb", "a\fb", "a\x1cb", "a\x1db", "a\x1eb",
    "a\u0085b", "a\u2028b", "a\u2029b", None, 123, [],
])
def test_invalid_names_leave_source_and_audit_unchanged(
    rename_context: tuple[TestClient, Session, Source, Path], name: object,
) -> None:
    client, db, source, _ = rename_context
    response = client.patch(f"/sources/{source.id}", json={"name": name})
    assert response.status_code == 422
    db.refresh(source)
    assert source.name == "sample.txt"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "source.renamed")) is None


def test_rename_is_wiki_scoped_and_unknown_sources_are_not_exposed(
    rename_context: tuple[TestClient, Session, Source, Path],
) -> None:
    client, db, source, _ = rename_context
    other = PersonalWiki(owner_id=source.created_by_id, name="Other Wiki")
    db.add(other)
    db.commit()
    client.app.dependency_overrides[get_current_wiki] = lambda: other
    for source_id in (source.id, "missing-source"):
        response = client.patch(f"/sources/{source_id}", json={"name": "not allowed"})
        assert response.status_code == 404
        assert response.json()["detail"] == "Source not found"
    db.refresh(source)
    assert source.name == "sample.txt"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "source.renamed")) is None


def test_rename_rejects_other_source_fields(
    rename_context: tuple[TestClient, Session, Source, Path],
) -> None:
    client, _, source, _ = rename_context
    response = client.patch(f"/sources/{source.id}", json={"name": "name", "kind": "website"})
    assert response.status_code == 422
    assert source.kind == SourceKind.TEXT


def test_rename_database_failure_rolls_back_and_reports_failure(
    rename_context: tuple[TestClient, Session, Source, Path],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, db, source, _ = rename_context
    source_id = source.id

    def fail_commit() -> None:
        raise SQLAlchemyError("private database detail")

    monkeypatch.setattr(db, "commit", fail_commit)
    response = client.patch(f"/sources/{source_id}", json={"name": "changed"})
    assert response.status_code == 500
    assert response.json()["detail"] == "Source rename failed; refresh and retry"
    db.refresh(source)
    assert source.name == "sample.txt"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "source.renamed")) is None
    assert "Source rename failed" in caplog.text
