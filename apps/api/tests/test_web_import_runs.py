from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from wiki_agent import tasks
from wiki_agent.api.logs import list_logs
from wiki_agent.config import Settings
from wiki_agent.db import Base
from wiki_agent.models import AgentRun, AuditLog, PersonalWiki, Source, SourceRevision, User
from wiki_agent.sources.article import extract_web_article
from wiki_agent.sources.crawler import CrawledPage, CrawlError, CrawlResult


@pytest.mark.parametrize(("valid_page", "too_large"), [(False, False), (True, False), (True, True)])
def test_crawl_runs_do_not_report_empty_imports_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valid_page: bool, too_large: bool,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None, storage_root=tmp_path / "storage",
        max_upload_bytes=20 if too_large else 5 * 1024 * 1024,
    )
    body = (
        b"<article><h1>Article title</h1>"
        b"<p>A complete article about reliable imports.</p></article>"
    )
    page = CrawledPage(
        "https://example.com/", "https://example.com/", "text/html", body,
        extract_web_article(body, "https://example.com/"), "http",
    )
    result = CrawlResult(
        pages=(page,) if valid_page else (),
        errors=(CrawlError("https://example.com/private", "请使用浏览器登录。", "login_required"),),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "crawl_site", lambda *args, **kwargs: result)
    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.flush()
        run = AgentRun(
            wiki_id=wiki.id, created_by_id=user.id, kind="crawl_site",
            input_json={"url": "https://example.com/"},
        )
        db.add(run)
        db.commit()
        imported_page = valid_page and not too_large
        if imported_page:
            completed = tasks._crawl_site(db, run.id)
            assert completed["status"] == "completed_with_errors"
        else:
            with pytest.raises(ValueError, match="没有导入有效正文"):
                tasks._crawl_site(db, run.id)
        db.refresh(run)
        assert run.status == ("completed_with_errors" if imported_page else "failed")
        assert run.result_json is not None
        assert run.result_json["imported_count"] == int(imported_page)
        assert run.result_json["failed_count"] == (2 if too_large else 1)
        assert run.result_json["browser_required"] is True
        imported = db.scalar(select(Source))
        failures = list(db.scalars(select(AuditLog).where(AuditLog.action == "source.web_failed")))
        assert len(failures) == (2 if too_large else 1)
        logs = list_logs(
            user, wiki, db, category="error", source="all", date_from=None,
            date_to=None, search=None, offset=0, limit=100,
        )
        assert any(entry["event"] == "source.web_failed" for entry in logs["items"])
        if imported_page:
            assert imported is not None
            assert imported.name == "Article title"
            revision = db.get(SourceRevision, imported.current_revision_id)
            assert revision is not None
            assert (settings.storage_root / revision.object_path).read_bytes() == body
            assert revision.metadata_json["article_title"] == "Article title"
        else:
            assert imported is None
