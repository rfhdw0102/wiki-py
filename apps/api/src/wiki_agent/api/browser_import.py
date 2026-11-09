import json
from collections.abc import AsyncIterator
from typing import Annotated

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from wiki_agent.api.dependencies import (
    AppSettings,
    CurrentUser,
    CurrentWiki,
    DbSession,
    require_feature,
)
from wiki_agent.api.schemas import SourceView, WebsiteSourceCreate
from wiki_agent.models import Source
from wiki_agent.sources.article import WebContentError, extract_web_article
from wiki_agent.sources.browser import (
    BrowserAction,
    BrowserClient,
    BrowserServiceError,
    BrowserSessionView,
)
from wiki_agent.sources.web_import import store_web_source

router = APIRouter(prefix="/sources/browser-sessions", tags=["sources"])


def browser_client(user: CurrentUser, wiki: CurrentWiki, settings: AppSettings) -> BrowserClient:
    return BrowserClient(settings, user.id, wiki.id)


CurrentBrowser = Annotated[BrowserClient, Depends(browser_client)]


def _browser_error(exc: WebContentError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code if isinstance(exc, BrowserServiceError) else 422,
        detail={"code": exc.code, "message": str(exc)},
    )


@router.post(
    "", response_model=BrowserSessionView, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("website_crawl"))],
)
def open_browser(payload: WebsiteSourceCreate, client: CurrentBrowser) -> BrowserSessionView:
    try:
        return client.open(str(payload.url))
    except BrowserServiceError as exc:
        raise _browser_error(exc) from exc


@router.get("/{session_id}", response_model=BrowserSessionView)
def get_browser(session_id: str, client: CurrentBrowser) -> BrowserSessionView:
    try:
        return client.get(session_id)
    except BrowserServiceError as exc:
        raise _browser_error(exc) from exc


@router.get("/{session_id}/screenshot")
def browser_screenshot(session_id: str, client: CurrentBrowser) -> Response:
    try:
        image = client.screenshot(session_id)
    except BrowserServiceError as exc:
        raise _browser_error(exc) from exc
    return Response(
        content=image, media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/{session_id}/frames")
async def browser_frames(
    session_id: str, client: CurrentBrowser, db: DbSession,
) -> StreamingResponse:
    db.rollback()
    try:
        upstream = await client.open_frames(session_id)
    except BrowserServiceError as exc:
        raise _browser_error(exc) from exc

    async def content() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.response.aiter_bytes():
                yield chunk
        except httpx.HTTPError:
            detail = {
                "code": "browser_stream_lost",
                "message": "实时网页连接已断开，请重新连接。",
            }
            yield f"event: error\ndata: {json.dumps(detail, ensure_ascii=True)}\n\n".encode()
        finally:
            with anyio.CancelScope(shield=True):
                await upstream.close()

    return StreamingResponse(
        content(), media_type="text/event-stream",
        background=BackgroundTask(upstream.close),
        headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/{session_id}/actions", response_model=BrowserSessionView)
def browser_action(
    session_id: str, payload: BrowserAction, client: CurrentBrowser,
) -> BrowserSessionView:
    try:
        return client.action(session_id, payload)
    except BrowserServiceError as exc:
        raise _browser_error(exc) from exc


@router.post(
    "/{session_id}/import", response_model=SourceView, status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("website_crawl"))],
)
def import_browser_article(
    session_id: str, client: CurrentBrowser, user: CurrentUser, wiki: CurrentWiki,
    db: DbSession, settings: AppSettings,
) -> Source:
    try:
        page = client.capture(session_id)
        article = extract_web_article(page.body, page.final_url, page.content_type)
        if len(page.body) > settings.max_upload_bytes:
            raise WebContentError("too_large", "网页超过当前允许的导入大小，未保存。")
        client.close(session_id)
    except WebContentError as exc:
        raise _browser_error(exc) from exc
    source, _ = store_web_source(
        db, settings, user_id=user.id, wiki_id=wiki.id,
        page=page, article=article, acquisition="interactive-browser",
    )
    db.commit()
    db.refresh(source)
    return source


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def close_browser(session_id: str, client: CurrentBrowser) -> None:
    try:
        client.close(session_id)
    except BrowserServiceError as exc:
        raise _browser_error(exc) from exc
