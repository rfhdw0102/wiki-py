import fnmatch
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import ParseResult, urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import AnyHttpUrl, BaseModel, Field

from wiki_agent.sources.article import WebArticle, WebContentError, extract_web_article
from wiki_agent.sources.fetcher import FetchedPage, UnsafeSourceUrlError, fetch_page

CRAWLER_USER_AGENT = "LLMWikiAgent"


class CrawlRequest(BaseModel):
    url: AnyHttpUrl
    max_depth: int = Field(default=0, ge=0, le=5)
    max_pages: int = Field(default=1, ge=1, le=100)
    include_patterns: list[str] = Field(default_factory=list, max_length=20)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=20)


@dataclass(frozen=True)
class CrawlPolicy:
    max_depth: int = 0
    max_pages: int = 1
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.max_depth <= 5:
            raise ValueError("max_depth must be between 0 and 5")
        if not 1 <= self.max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100")


@dataclass(frozen=True)
class CrawlError:
    url: str
    error: str
    code: str = "fetch_failed"


@dataclass(frozen=True)
class CrawledPage(FetchedPage):
    article: WebArticle
    acquisition: str


@dataclass(frozen=True)
class CrawlResult:
    pages: tuple[CrawledPage, ...]
    errors: tuple[CrawlError, ...]


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = next((value for key, value in attrs if key.lower() == "href"), None)
        if href:
            self.links.append(href)


def crawl_site(
    start_url: str, policy: CrawlPolicy,
    *, render_page: Callable[[str], FetchedPage] | None = None,
) -> CrawlResult:
    root = _canonicalize(start_url)
    root_parsed = urlparse(root)
    robots = _load_robots(root_parsed)
    queue = deque([(root, 0)])
    queued = {root}
    pages: list[CrawledPage] = []
    errors: list[CrawlError] = []

    attempts = 0
    while queue and attempts < policy.max_pages:
        url, depth = queue.popleft()
        attempts += 1
        if not robots.can_fetch(CRAWLER_USER_AGENT, url):
            errors.append(CrawlError(
                url=url, error="网站的采集规则禁止自动读取该页面。", code="robots_blocked",
            ))
            continue
        acquisition = "http"
        try:
            page = fetch_page(url)
            article = extract_web_article(page.body, page.final_url, page.content_type)
        except UnsafeSourceUrlError:
            errors.append(CrawlError(url, "该网址不是允许访问的公网地址。", "unsafe_url"))
            continue
        except (httpx.HTTPError, OSError, ValueError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                errors.append(CrawlError(url, "页面不存在或文章已删除。", "content_unavailable"))
                continue
            if render_page is None or (
                isinstance(exc, WebContentError)
                and exc.code in {"too_large", "unsupported_content", "content_unavailable"}
            ):
                errors.append(CrawlError(
                    url,
                    str(exc) if isinstance(exc, WebContentError) else "网页请求失败，请稍后重试。",
                    exc.code if isinstance(exc, WebContentError) else "fetch_failed",
                ))
                continue
            try:
                page = render_page(url)
                article = extract_web_article(page.body, page.final_url, page.content_type)
                acquisition = "browser"
            except (httpx.HTTPError, OSError, ValueError) as rendered_error:
                errors.append(CrawlError(
                    url,
                    str(rendered_error) if isinstance(rendered_error, WebContentError)
                    else "浏览器未能读取有效正文，请打开浏览器完成验证或登录后再导入。",
                    rendered_error.code if isinstance(rendered_error, WebContentError)
                    else "browser_required",
                ))
                continue
        pages.append(CrawledPage(
            requested_url=page.requested_url, final_url=page.final_url,
            content_type=page.content_type, body=page.body,
            article=article, acquisition=acquisition,
        ))
        if depth >= policy.max_depth or page.content_type not in {
            "text/html",
            "application/xhtml+xml",
        }:
            continue
        parser = _LinkParser()
        parser.feed(page.body.decode("utf-8", errors="replace"))
        for href in parser.links:
            candidate = _canonicalize(urljoin(page.final_url, href))
            if candidate in queued or not _same_origin(candidate, root):
                continue
            candidate_path = urlparse(candidate).path or "/"
            if policy.include_patterns and not any(
                fnmatch.fnmatch(candidate_path, pattern) for pattern in policy.include_patterns
            ):
                continue
            if any(fnmatch.fnmatch(candidate_path, pattern) for pattern in policy.exclude_patterns):
                continue
            queued.add(candidate)
            queue.append((candidate, depth + 1))
            if len(queued) >= policy.max_pages:
                break
    return CrawlResult(pages=tuple(pages), errors=tuple(errors))


def _load_robots(root: ParseResult) -> RobotFileParser:
    robots_url = f"{root.scheme}://{root.netloc}/robots.txt"
    parser = RobotFileParser()
    parser.set_url(robots_url)
    try:
        response = fetch_page(robots_url, max_bytes=512 * 1024)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        parser.parse([])
    else:
        parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
    return parser


def _canonicalize(url: str) -> str:
    without_fragment, _ = urldefrag(url)
    parsed = urlparse(without_fragment)
    normalized_path = parsed.path or "/"
    return parsed._replace(path=normalized_path, fragment="").geturl()


def _same_origin(candidate: str, root: str) -> bool:
    candidate_url = urlparse(candidate)
    root_url = urlparse(root)
    return (
        candidate_url.scheme == root_url.scheme
        and candidate_url.hostname == root_url.hostname
        and candidate_url.port == root_url.port
    )
