from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.api.schemas import WikiCreate
from wiki_agent.api.wikis import create_wiki, delete_wiki, initialize_wiki, list_wikis, router
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import Base, get_db
from wiki_agent.models import (
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    UserRole,
    WikiDocument,
)
from wiki_agent.search.repository import RawSearchRepository, WikiSearchRepository
from wiki_agent.wiki import WikiRepository, open_wiki_repository


def add_user(db: Session, email: str) -> User:
    user = User(
        email=email,
        display_name=email,
        password_hash="unused",
        role=UserRole.USER,
    )
    db.add(user)
    db.flush()
    return user


def add_raw_source(
    db: Session,
    *,
    user: User,
    wiki: PersonalWiki,
    suffix: str,
    content: str,
    settings: Settings | None = None,
) -> None:
    source = Source(
        wiki_id=wiki.id,
        name=f"{suffix}.txt",
        kind=SourceKind.TEXT,
        created_by_id=user.id,
    )
    db.add(source)
    db.flush()
    object_path = f"sources/{suffix}/object"
    if settings is not None:
        path = settings.source_objects_root / suffix[:2] / (suffix * 64)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        object_path = str(path.resolve().relative_to(settings.storage_root.resolve()))
    revision = SourceRevision(
        id=f"revision-{suffix}",
        source_id=source.id,
        sha256=suffix * 64,
        object_path=object_path,
        media_type="text/plain",
        size_bytes=len(content),
    )
    db.add(revision)
    db.flush()
    db.add(
        SourceSpan(
            id=f"{revision.id}:0",
            source_revision_id=revision.id,
            ordinal=0,
            content=content,
            locator={},
        )
    )


def test_personal_wikis_are_owned_and_use_the_shared_repository(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge")
    with Session(engine) as db:
        owner = add_user(db, "owner@qq.com")
        other = add_user(db, "other@qq.com")
        db.commit()

        wiki = create_wiki(
            WikiCreate(name="技术笔记", description="个人资料"),
            owner,
            db,
            settings,
        )

        assert list_wikis(owner, db) == [wiki]
        assert list_wikis(other, db) == []
        assert settings.wiki_library_root(wiki.id) == settings.knowledge_root
        assert settings.wiki_library_root(wiki.id).joinpath(".git").is_dir()
        assert settings.legacy_wiki_library_root(wiki.id) != settings.wiki_library_root(wiki.id)
        with pytest.raises(HTTPException) as duplicate:
            create_wiki(WikiCreate(name="另一个 Wiki"), owner, db, settings)
        assert duplicate.value.status_code == 409
        with pytest.raises(HTTPException) as exc:
            get_current_wiki(other, db, wiki.id)
        assert exc.value.status_code == 404


def test_search_results_are_isolated_by_wiki() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = add_user(db, "owner@qq.com")
        first = PersonalWiki(owner_id=user.id, name="技术")
        second = PersonalWiki(owner_id=user.id, name="旅行")
        db.add_all([first, second])
        db.flush()
        add_raw_source(
            db,
            user=user,
            wiki=first,
            suffix="a",
            content="Python type checking handbook",
        )
        add_raw_source(
            db,
            user=user,
            wiki=second,
            suffix="b",
            content="Kyoto travel handbook",
        )
        db.add_all(
            [
                WikiDocument(
                    wiki_id=first.id,
                    path="concepts/python.md",
                    page_id="python",
                    page_type="concept",
                    title="Python",
                    content="Python type checking handbook",
                    commit_sha="a" * 40,
                ),
                WikiDocument(
                    wiki_id=second.id,
                    path="concepts/kyoto.md",
                    page_id="kyoto",
                    page_type="concept",
                    title="Kyoto",
                    content="Kyoto travel handbook",
                    commit_sha="b" * 40,
                ),
            ]
        )
        db.commit()

        raw_hits = RawSearchRepository(db, first.id).bm25("handbook")
        wiki_hits = WikiSearchRepository(db, first.id).bm25("handbook")

    assert [hit.document_id for hit in raw_hits] == ["raw:revision-a:0"]
    assert [hit.document_id for hit in wiki_hits] == ["concepts/python.md"]


def test_initializing_personal_wiki_requires_no_name_and_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge")
    with Session(engine) as db:
        owner = add_user(db, "owner@qq.com")
        other = add_user(db, "other@qq.com")
        db.commit()

        first = initialize_wiki(owner, db, settings)
        repeated = initialize_wiki(owner, db, settings)
        other_wiki = initialize_wiki(other, db, settings)

        assert first.id == repeated.id
        assert first.name == "个人知识库"
        assert first.description == ""
        assert list_wikis(owner, db) == [first]
        assert list_wikis(other, db) == [other_wiki]
        assert other_wiki.id != first.id
        assert settings.knowledge_root.joinpath(".git").is_dir()


def test_initializing_existing_wiki_preserves_its_name_and_sources(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        knowledge_root=tmp_path / "knowledge",
        storage_root=tmp_path / "storage",
    )
    with Session(engine) as db:
        user = add_user(db, "owner@qq.com")
        wiki = create_wiki(WikiCreate(name="旧库名", description="原有简介"), user, db, settings)
        add_raw_source(db, user=user, wiki=wiki, suffix="a", content="existing", settings=settings)
        db.commit()
        source_id = db.scalar(select(Source.id).where(Source.wiki_id == wiki.id))
        repository = WikiRepository(settings.knowledge_root)
        original_commit = repository.head()
        obsolete = PersonalWiki(owner_id=user.id, name="历史库")
        db.add(obsolete)
        db.commit()

        result = initialize_wiki(user, db, settings)

        assert result.id == wiki.id
        assert result.name == "旧库名"
        assert result.description == "原有简介"
        assert db.get(PersonalWiki, obsolete.id) is not None
        assert source_id is not None
        assert db.get(Source, source_id) is not None
        assert settings.source_objects_root.joinpath("a", "a" * 64).read_text() == "existing"
        assert repository.head() == original_commit


def test_initialization_failure_rolls_back_the_new_wiki(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge")

    def fail_repository(*args: object, **kwargs: object) -> None:
        raise OSError("repository unavailable")

    with Session(engine) as db:
        user = add_user(db, "owner@qq.com")
        db.commit()
        monkeypatch.setattr("wiki_agent.api.wikis.open_wiki_repository", fail_repository)

        with pytest.raises(OSError, match="repository unavailable"):
            initialize_wiki(user, db, settings)

        assert list_wikis(user, db) == []


def test_initialization_endpoint_accepts_no_body_and_requires_authentication(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with Session(engine) as db:
        user = add_user(db, "owner@qq.com")
        db.commit()
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_settings] = lambda: settings
        with TestClient(app) as client:
            assert client.post("/api/v1/wikis/initialize").status_code == 401
            app.dependency_overrides[get_current_user] = lambda: user
            first = client.post("/api/v1/wikis/initialize")
            repeated = client.post("/api/v1/wikis/initialize")

        assert first.status_code == repeated.status_code == 200
        assert first.json() == repeated.json()
        assert first.json()["name"] == "个人知识库"
        assert first.json()["description"] == ""
        assert first.json()["id"] == list_wikis(user, db)[0].id
        assert len(list_wikis(user, db)) == 1


def test_deleting_wiki_cascades_to_its_sources(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        knowledge_root=tmp_path / "knowledge",
        storage_root=tmp_path / "storage",
    )

    with Session(engine) as db:
        user = add_user(db, "owner@qq.com")
        db.commit()
        wiki = create_wiki(
            WikiCreate(name="待删除", description=""),
            user,
            db,
            settings,
        )
        add_raw_source(
            db,
            user=user,
            wiki=wiki,
            suffix="c",
            content="temporary content",
            settings=settings,
        )
        source_id = db.scalar(select(Source.id).where(Source.wiki_id == wiki.id))
        db.commit()

        delete_wiki(wiki.id, user, db, settings)

        assert db.get(PersonalWiki, wiki.id) is None
        assert source_id is not None
        assert db.get(Source, source_id) is None
        assert settings.knowledge_root.joinpath(".git").is_dir()


def test_deleting_wiki_removes_only_unshared_original_files(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None,
        knowledge_root=tmp_path / "knowledge",
        storage_root=tmp_path / "storage",
    )
    with Session(engine) as db:
        user = add_user(db, "owner@qq.com")
        retained = create_wiki(WikiCreate(name="当前"), user, db, settings)
        obsolete = PersonalWiki(owner_id=user.id, name="旧库")
        db.add(obsolete)
        db.flush()
        obsolete_root = settings.wiki_library_root(obsolete.id)
        WikiRepository(obsolete_root).initialize()
        raw_path = settings.source_objects_root / "ab" / ("a" * 64)
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text("original")
        for wiki, suffix in [(retained, "kept"), (obsolete, "removed")]:
            source = Source(
                wiki_id=wiki.id,
                name=f"{suffix}.txt",
                kind=SourceKind.TEXT,
                created_by_id=user.id,
            )
            db.add(source)
            db.flush()
            db.add(
                SourceRevision(
                    source_id=source.id,
                    sha256="a" * 64,
                    object_path=str(raw_path.relative_to(settings.storage_root)),
                    media_type="text/plain",
                    size_bytes=8,
                )
            )
        db.commit()

        delete_wiki(obsolete.id, user, db, settings)
        assert db.get(PersonalWiki, retained.id) is not None
        assert obsolete_root == settings.knowledge_root
        assert obsolete_root.joinpath(".git").is_dir()
        assert raw_path.exists()

        delete_wiki(retained.id, user, db, settings)
        assert not raw_path.exists()


def test_legacy_wiki_pages_migrate_into_the_shared_repository(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, knowledge_root=tmp_path / "knowledge")
    wiki_id = "legacy-wiki-id"
    legacy_root = settings.legacy_wiki_library_root(wiki_id)
    legacy = WikiRepository(legacy_root)
    legacy.initialize()
    legacy.publish(
        {
            "sources/revision-1.md": (
                "---\nid: source-revision-1\ntype: source\ntitle: Source\n"
                "created: 2026-09-28T00:00:00Z\nupdated: 2026-09-28T00:00:00Z\n"
                "---\n\nOriginal source summary."
            )
        },
        base_commit=legacy.head(),
        author_name="Owner",
        author_email="owner@qq.com",
        message="Publish source page",
    )

    shared = open_wiki_repository(
        settings.wiki_library_root(wiki_id),
        legacy_root,
    )

    assert shared.list_pages() == ["sources/revision-1.md"]
    assert shared.read_page("sources/revision-1.md").metadata.title == "Source"
    assert legacy_root.joinpath(".git").is_dir()
    assert "/wikis/" in settings.knowledge_root.joinpath(".gitignore").read_text()
    assert "/wikis/" in shared._run("show", "HEAD:.gitignore").stdout
    migrated_revision = shared.head()
    assert shared.migrate_legacy_pages(legacy_root) is None
    assert shared.head() == migrated_revision
