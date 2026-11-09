import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import quote

import anyio
import httpx
import jwt
from pydantic import BaseModel, Field, ValidationError, model_validator

from wiki_agent.config import Settings
from wiki_agent.sources.article import MAX_WEB_BYTES, WebContentError
from wiki_agent.sources.fetcher import FetchedPage


class BrowserServiceError(WebContentError):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(code, message)
        self.status_code = status_code


class BrowserSessionView(BaseModel):
    id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    url: str = Field(max_length=8192)
    title: str = Field(max_length=512)
    width: int = Field(ge=1, le=2048)
    height: int = Field(ge=1, le=2048)
    expires_at: datetime


class BrowserSnapshot(BaseModel):
    url: str = Field(max_length=8192)
    title: str = Field(max_length=512)
    html: str = Field(max_length=MAX_WEB_BYTES)


class BrowserAction(BaseModel):
    action: Literal["click", "drag", "pointer", "type", "key", "scroll", "reload", "back"]
    phase: Literal["down", "move", "up"] | None = None
    x: int | None = Field(default=None, ge=0, lt=1024)
    y: int | None = Field(default=None, ge=0, lt=768)
    to_x: int | None = Field(default=None, ge=0, lt=1024)
    to_y: int | None = Field(default=None, ge=0, lt=768)
    text: str | None = Field(default=None, max_length=1000)
    key: Literal[
        "Enter", "Tab", "Backspace", "Escape", "ArrowLeft", "ArrowRight",
        "ArrowUp", "ArrowDown", "Control+A", "Meta+A",
    ] | None = None
    delta: int | None = Field(default=None, ge=-1200, le=1200)

    @model_validator(mode="after")
    def validate_action(self) -> "BrowserAction":
        if self.action == "click" and (self.x is None or self.y is None):
            raise ValueError("click requires x and y")
        if self.action == "pointer" and (
            self.phase is None or self.x is None or self.y is None
        ):
            raise ValueError("pointer requires phase, x and y")
        if self.action == "drag" and any(
            value is None for value in (self.x, self.y, self.to_x, self.to_y)
        ):
            raise ValueError("drag requires start and end coordinates")
        if self.action == "type" and self.text is None:
            raise ValueError("type requires text")
        if self.action == "key" and self.key is None:
            raise ValueError("key requires an allowed key")
        if self.action == "scroll" and self.delta is None:
            raise ValueError("scroll requires delta")
        return self


ERROR_MESSAGES = {
    "SESSION_EXPIRED": "浏览器会话已过期，请重新打开。",
    "SESSION_NOT_FOUND": "浏览器会话已结束，请重新打开。",
    "SESSION_NOT_OWNED": "无法访问其他账号的浏览器会话。",
    "BROWSER_CAPACITY": "浏览器正忙，请关闭不再使用的窗口后重试。",
    "PUBLIC_URL_REQUIRED": "只允许访问公网 HTTP 或 HTTPS 网址。",
    "UNSAFE_URL": "该网址不是允许访问的公网地址。",
    "BROWSER_RENDER_FAILED": "浏览器未能打开该页面，请检查网址后重试。",
    "NO_HISTORY": "当前网页没有可返回的上一页。",
}


@dataclass
class BrowserFrameStream:
    client: httpx.AsyncClient
    response: httpx.Response

    async def close(self) -> None:
        try:
            await self.response.aclose()
        finally:
            await self.client.aclose()


class BrowserClient:
    def __init__(
        self, settings: Settings, user_id: str, wiki_id: str,
        *, transport: httpx.BaseTransport | None = None,
        async_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.user_id = user_id
        self.wiki_id = wiki_id
        self.transport = transport
        self.async_transport = async_transport

    def _authorization(self, session_id: str | None = None) -> dict[str, str]:
        now = datetime.now(UTC)
        claims: dict[str, object] = {
            "sub": self.user_id, "wiki_id": self.wiki_id,
            "aud": "wiki-browser-import", "iss": "wiki-agent",
            "exp": now + timedelta(seconds=60),
        }
        if session_id is not None:
            claims.update({
                "scope": "browser-frames", "session_id": session_id,
                "iat": now, "exp": now + timedelta(minutes=15),
            })
        token = jwt.encode(
            claims,
            self.settings.secret_key, algorithm="HS256",
        )
        return {"authorization": f"Bearer {token}"}

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        if not self.settings.browser_service_url:
            raise BrowserServiceError(
                "browser_unavailable", "浏览器服务未启用，请启动 browser 服务后重试。", 503,
            )
        try:
            with httpx.Client(
                base_url=self.settings.browser_service_url.rstrip("/") + "/",
                timeout=45, trust_env=False, transport=self.transport,
            ) as client:
                response = client.request(
                    method, path.lstrip("/"), json=payload,
                    headers=self._authorization(),
                )
        except httpx.HTTPError as exc:
            raise BrowserServiceError(
                "browser_unavailable", "无法连接浏览器服务，请检查 browser 服务后重试。", 503,
            ) from exc
        self._check_response(response)
        return response

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if not response.is_success:
            code = "BROWSER_SERVICE_FAILED"
            try:
                data = response.json()
                detail = data.get("detail") if isinstance(data, dict) else None
                if isinstance(detail, dict) and isinstance(detail.get("code"), str):
                    code = detail["code"].upper()
            except ValueError:
                code = "BROWSER_SERVICE_FAILED"
            raise BrowserServiceError(
                code.lower(), ERROR_MESSAGES.get(code, "浏览器操作失败，请重试或重新打开浏览器。"),
                response.status_code if response.status_code in {400, 403, 404, 409, 429, 503}
                else 502,
            )

    async def open_frames(self, session_id: str) -> BrowserFrameStream:
        path = self._session_path(session_id) + "/frames"
        if not self.settings.browser_service_url:
            raise BrowserServiceError(
                "browser_unavailable", "实时网页服务未启用，请启动 browser 服务后重试。", 503,
            )
        client = httpx.AsyncClient(
            base_url=self.settings.browser_service_url.rstrip("/") + "/",
            timeout=httpx.Timeout(10, read=30), trust_env=False,
            transport=self.async_transport,
        )
        try:
            request = client.build_request(
                "GET", path.lstrip("/"), headers=self._authorization(session_id),
            )
            response = await client.send(request, stream=True)
            if not response.is_success:
                await response.aread()
                self._check_response(response)
            if response.headers.get("content-type", "").split(";")[0] != "text/event-stream":
                await response.aclose()
                raise BrowserServiceError(
                    "invalid_browser_response", "无法读取实时网页画面，请重新连接。",
                )
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                await client.aclose()
            raise
        except (httpx.HTTPError, BrowserServiceError) as exc:
            await client.aclose()
            if isinstance(exc, BrowserServiceError):
                raise
            raise BrowserServiceError(
                "browser_unavailable", "实时网页连接失败，请检查浏览器服务后重试。", 503,
            ) from exc
        return BrowserFrameStream(client, response)

    @staticmethod
    def _session_path(session_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", session_id):
            raise BrowserServiceError("invalid_session", "浏览器会话标识无效。", 400)
        return f"/sessions/{quote(session_id, safe='')}"

    @staticmethod
    def _session(response: httpx.Response) -> BrowserSessionView:
        try:
            return BrowserSessionView.model_validate(response.json())
        except (ValueError, ValidationError):
            raise BrowserServiceError(
                "invalid_browser_response", "浏览器返回了无效的会话信息，请重新打开。",
            ) from None

    @staticmethod
    def _snapshot(response: httpx.Response, requested_url: str) -> FetchedPage:
        try:
            snapshot = BrowserSnapshot.model_validate(response.json())
        except (ValueError, ValidationError):
            raise BrowserServiceError(
                "invalid_browser_response", "浏览器返回了无效的页面内容，未导入。",
            ) from None
        body = snapshot.html.encode("utf-8")
        if len(body) > MAX_WEB_BYTES:
            raise BrowserServiceError("too_large", "网页超过 5 MB，未导入。", 413)
        return FetchedPage(requested_url, snapshot.url, "text/html", body)

    def render(self, url: str) -> FetchedPage:
        return self._snapshot(self._request("POST", "/render", {"url": url}), url)

    def open(self, url: str) -> BrowserSessionView:
        return self._session(self._request("POST", "/sessions", {"url": url}))

    def get(self, session_id: str) -> BrowserSessionView:
        return self._session(self._request("GET", self._session_path(session_id)))

    def action(self, session_id: str, action: BrowserAction) -> BrowserSessionView:
        return self._session(self._request(
            "POST", self._session_path(session_id) + "/actions",
            action.model_dump(exclude_none=True),
        ))

    def screenshot(self, session_id: str) -> bytes:
        response = self._request("GET", self._session_path(session_id) + "/screenshot")
        if (
            response.headers.get("content-type", "").split(";")[0] != "image/png"
            or len(response.content) > MAX_WEB_BYTES
            or not response.content.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            raise BrowserServiceError("invalid_screenshot", "无法读取浏览器画面，请刷新后重试。")
        return response.content

    def capture(self, session_id: str) -> FetchedPage:
        return self._snapshot(
            self._request("GET", self._session_path(session_id) + "/capture"), "",
        )

    def close(self, session_id: str) -> None:
        response = self._request("DELETE", self._session_path(session_id))
        if response.status_code != 204:
            raise BrowserServiceError("invalid_browser_response", "无法确认浏览器已关闭，请重试。")
