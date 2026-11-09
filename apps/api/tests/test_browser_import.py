import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from wiki_agent.api import browser_import
from wiki_agent.api import sources as source_api
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import Base, get_db
from wiki_agent.models import FeatureFlag, PersonalWiki, Source, SourceRevision, User
from wiki_agent.sources.browser import (
    BrowserAction,
    BrowserClient,
    BrowserServiceError,
)
from wiki_agent.sources.fetcher import FetchedPage

SESSION_ID = "a" * 48
PNG = b"\x89PNG\r\n\x1a\nfixture"
SESSION = {
    "id": SESSION_ID, "url": "https://example.com/article",
    "title": "Article", "width": 1024, "height": 768,
    "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
}
ARTICLE = (
    b"<article><h1>Browser article</h1><p>The browser returned a complete article "
    b"after the user finished their manual verification.</p></article>"
)


def test_service_requests_sign_owner_and_wiki_scope_without_passing_credentials_to_urls() -> None:
    settings = Settings(_env_file=None, browser_service_url="http://browser.test:8080")
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        claims = jwt.decode(
            request.headers["authorization"].removeprefix("Bearer "),
            settings.secret_key, algorithms=["HS256"], audience="wiki-browser-import",
            issuer="wiki-agent",
        )
        assert claims["sub"] == "owner"
        assert claims["wiki_id"] == "wiki"
        assert "scope" not in claims
        assert request.url.host == "browser.test"
        return httpx.Response(201, json=SESSION)

    client = BrowserClient(settings, "owner", "wiki", transport=httpx.MockTransport(transport))
    assert client.open("https://example.com/article").id == SESSION_ID
    assert json.loads(requests[0].content) == {"url": "https://example.com/article"}


def test_invalid_service_responses_never_leak_page_or_login_content() -> None:
    settings = Settings(_env_file=None, browser_service_url="http://browser.test:8080")
    client = BrowserClient(settings, "owner", "wiki", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            "url": "https://example.com/", "title": "Title",
            "html": {"password": "SECRET_TEST_MARKER"},
        })
    ))
    with pytest.raises(BrowserServiceError) as failure:
        client.render("https://example.com/")
    assert "SECRET_TEST_MARKER" not in str(failure.value)


@pytest.mark.parametrize("action", [
    {"action": "click", "x": 1024, "y": 1},
    {"action": "drag", "x": 0, "y": 0},
    {"action": "key", "key": "ExecuteScript"},
    {"action": "scroll", "delta": 1201},
    {"action": "pointer", "x": 1, "y": 2},
    {"action": "pointer", "phase": "move", "x": -1, "y": 2},
])
def test_browser_actions_are_bounded_and_not_executable_commands(action: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BrowserAction.model_validate(action)
    assert BrowserAction(action="drag", x=10, y=10, to_x=100, to_y=10).to_x == 100
    assert BrowserAction(action="pointer", phase="down", x=10, y=10).phase == "down"


@pytest.mark.parametrize(("captcha", "too_large"), [(False, False), (True, False), (False, True)])
def test_browser_capture_saves_only_valid_article_and_closes_session_after_validation(
    tmp_path: Path, captcha: bool, too_large: bool,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    settings = Settings(
        _env_file=None, storage_root=tmp_path / "storage",
        max_upload_bytes=20 if too_large else 5 * 1024 * 1024,
    )
    closed: list[str] = []

    class FakeBrowser(BrowserClient):
        def capture(self, session_id: str) -> FetchedPage:
            assert session_id == SESSION_ID
            return FetchedPage(
                "", "https://example.com/article", "text/html",
                b"<title>Security check</title><p>Please verify you are human.</p>"
                if captcha else ARTICLE,
            )

        def close(self, session_id: str) -> None:
            closed.append(session_id)

        def screenshot(self, session_id: str) -> bytes:
            return PNG

    with Session(engine) as db:
        user = User(display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        db.add(wiki)
        db.add(FeatureFlag(key="website_crawl", enabled=True))
        db.commit()
        fake = FakeBrowser(settings, user.id, wiki.id)
        app = FastAPI()
        app.include_router(browser_import.router)
        app.include_router(source_api.router)
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_wiki] = lambda: wiki
        app.dependency_overrides[get_settings] = lambda: settings
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[browser_import.browser_client] = lambda: fake
        with TestClient(app) as client:
            image = client.get(f"/sources/browser-sessions/{SESSION_ID}/screenshot")
            assert image.content == PNG
            assert image.headers["cache-control"] == "no-store"
            response = client.post(f"/sources/browser-sessions/{SESSION_ID}/import")
        if captcha or too_large:
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == (
                "verification_required" if captcha else "too_large"
            )
            assert closed == []
            assert db.scalar(select(Source.id)) is None
        else:
            assert response.status_code == 201
            assert response.json()["name"] == "Browser article"
            assert closed == [SESSION_ID]
            source = db.scalar(select(Source))
            assert source is not None
            revision = db.get(SourceRevision, source.current_revision_id)
            assert revision is not None
            assert revision.metadata_json["acquisition"] == "interactive-browser"
            assert (settings.storage_root / revision.object_path).read_bytes() == ARTICLE
            with TestClient(app) as client:
                original = client.get(f"/sources/revisions/{revision.id}/content")
                preview = client.get(f"/sources/revisions/{revision.id}/content?preview=true")
            assert original.content == ARTICLE
            assert preview.status_code == 200
            assert preview.headers["content-type"].startswith("text/markdown")
            assert "complete article" in preview.text
            assert "<article>" not in preview.text


def test_browser_input_validation_does_not_echo_credentials() -> None:
    from wiki_agent.main import validation_error_response

    app = FastAPI()
    app.include_router(browser_import.router, prefix="/api/v1")
    app.add_exception_handler(RequestValidationError, validation_error_response)
    app.dependency_overrides[browser_import.browser_client] = lambda: BrowserClient(
        Settings(_env_file=None), "owner", "wiki",
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/sources/browser-sessions/{SESSION_ID}/actions",
            json={"action": "type", "text": "SECRET_TEST_MARKER" * 100},
        )
    assert response.status_code == 422
    assert "SECRET_TEST_MARKER" not in response.text


@pytest.mark.parametrize("broken", [False, True])
def test_live_frame_proxy_releases_database_transaction_and_closes_upstream(broken: bool) -> None:
    closed: list[bool] = []
    expected = b"event: frame\ndata: {\"image\":\"fixture\"}\n\n"

    class LiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield expected
            if broken:
                raise httpx.ReadError("SECRET_TEST_MARKER")

        async def aclose(self) -> None:
            closed.append(True)

    engine = create_engine(
        "sqlite+pysqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    with Session(engine) as db:
        db.execute(select(1))
        assert db.in_transaction()

        async def transport(request: httpx.Request) -> httpx.Response:
            assert not db.in_transaction()
            assert request.url.path == f"/sessions/{SESSION_ID}/frames"
            claims = jwt.decode(
                request.headers["authorization"].removeprefix("Bearer "),
                settings.secret_key, algorithms=["HS256"], audience="wiki-browser-import",
                issuer="wiki-agent",
            )
            assert claims["sub"] == "owner"
            assert claims["scope"] == "browser-frames"
            assert claims["session_id"] == SESSION_ID
            assert claims["exp"] - claims["iat"] == 900
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=LiveStream(),
            )

        settings = Settings(_env_file=None, browser_service_url="http://browser.test:8080")
        browser = BrowserClient(
            settings, "owner", "wiki", async_transport=httpx.MockTransport(transport),
        )
        app = FastAPI()
        app.include_router(browser_import.router)
        app.dependency_overrides[browser_import.browser_client] = lambda: browser
        app.dependency_overrides[get_db] = lambda: db
        with TestClient(app) as client:
            response = client.get(f"/sources/browser-sessions/{SESSION_ID}/frames")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-accel-buffering"] == "no"
        assert response.content.startswith(expected)
        assert "SECRET_TEST_MARKER" not in response.text
        assert ("browser_stream_lost" in response.text) == broken
        assert closed == [True]


def test_live_stream_rejects_wrong_owner_before_success_headers() -> None:
    settings = Settings(_env_file=None, browser_service_url="http://browser.test:8080")
    browser = BrowserClient(
        settings, "owner", "wiki", async_transport=httpx.MockTransport(
            lambda request: httpx.Response(404, json={
                "detail": {"code": "SESSION_NOT_OWNED", "message": "SECRET_TEST_MARKER"},
            })
        ),
    )

    async def reject() -> None:
        with pytest.raises(BrowserServiceError) as failure:
            await browser.open_frames(SESSION_ID)
        assert failure.value.code == "session_not_owned"
        assert failure.value.status_code == 404
        assert "SECRET_TEST_MARKER" not in str(failure.value)

    asyncio.run(reject())
