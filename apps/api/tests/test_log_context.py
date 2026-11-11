import json

from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.api.log_context import enrich_log_context
from wiki_agent.api.logs import list_logs
from wiki_agent.db import Base
from wiki_agent.models import (
    AgentRun,
    AuditLog,
    Draft,
    DraftStatus,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    User,
    WikiDocument,
)


def test_log_links_resolve_owned_originals_and_real_page_titles_without_cross_wiki_data() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        other = User(display_name="Other", password_hash="unused")
        db.add_all([user, other])
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        foreign = PersonalWiki(owner_id=other.id, name="Private")
        db.add_all([wiki, foreign])
        db.flush()
        source = Source(
            wiki_id=wiki.id, created_by_id=user.id, name="真实原件.md", kind=SourceKind.MARKDOWN
        )
        private_source = Source(
            wiki_id=foreign.id, created_by_id=other.id, name="PRIVATE_NAME", kind=SourceKind.TEXT
        )
        db.add_all([source, private_source])
        db.flush()
        revision = SourceRevision(
            source_id=source.id,
            sha256="a" * 64,
            object_path="unused",
            media_type="text/markdown",
            size_bytes=10,
        )
        private_revision = SourceRevision(
            source_id=private_source.id,
            sha256="b" * 64,
            object_path="unused",
            media_type="text/plain",
            size_bytes=10,
        )
        db.add_all([revision, private_revision])
        db.flush()
        source.current_revision_id = revision.id
        draft = Draft(
            wiki_id=wiki.id,
            created_by_id=user.id,
            base_commit="base",
            title="草稿",
            status=DraftStatus.PUBLISHED,
            files={"entities/page.md": "content"},
        )
        db.add(draft)
        db.add_all(
            [
                WikiDocument(
                    wiki_id=wiki.id,
                    path="entities/page.md",
                    page_id="page",
                    page_type="entity",
                    title="真正的页面标题",
                    content="body",
                    commit_sha="hash",
                ),
                WikiDocument(
                    wiki_id=foreign.id,
                    path="entities/private.md",
                    page_id="private",
                    page_type="entity",
                    title="PRIVATE_TITLE",
                    content="body",
                    commit_sha="hash",
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                AuditLog(
                    actor_id=user.id,
                    action="source.uploaded",
                    resource_type="source_revision",
                    resource_id=revision.id,
                    details={"name": "真实原件.md"},
                ),
                AuditLog(
                    actor_id=user.id,
                    action="draft.published",
                    resource_type="draft",
                    resource_id=draft.id,
                    details={"paths": ["entities/page.md", "entities/private.md"]},
                ),
            ]
        )
        db.commit()
        result = list_logs(user, wiki, db, offset=0, limit=100, search=None)
        original = next(item for item in result["items"] if item["event"] == "source.uploaded")
        published = next(item for item in result["items"] if item["event"] == "draft.published")
        assert original["related"][0]["label"] == "真实原件.md"
        assert original["related"][0]["href"] == f"/sources?revision={revision.id}"
        assert original["source_saved"] is True
        assert published["page_count"] == 2
        assert published["related"] == [
            {
                "label": "真正的页面标题",
                "href": "/wiki?page=entities%2Fpage.md",
                "kind": "page",
            }
        ]
        raw = dict(original, resource_id=private_revision.id, related=[], resource_name=None)
        enrich_log_context([raw], db, wiki.id, user.id)
        assert raw["related"] == []
        assert "PRIVATE_NAME" not in json.dumps(jsonable_encoder(raw))
        assert "PRIVATE_TITLE" not in json.dumps(jsonable_encoder(result))


def test_published_index_failure_remains_visible_and_independent_failures_are_not_merged() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.flush()
        for _ in range(2):
            db.add(
                AgentRun(
                    wiki_id=wiki.id,
                    created_by_id=user.id,
                    kind="compile_source",
                    status="published_index_failed",
                    input_json={},
                    error="Index unavailable",
                )
            )
        db.commit()
        result = list_logs(user, wiki, db, category="error", search=None, offset=0, limit=100)
        assert result["total"] == 2
        assert len({item["id"] for item in result["items"]}) == 2
        assert all(
            item["processing_status"] == "published_index_failed" for item in result["items"]
        )
