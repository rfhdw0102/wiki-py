import pytest
from wiki_agent.sources import crawler
from wiki_agent.sources.crawler import CrawlPolicy, CrawlRequest, crawl_site
from wiki_agent.sources.fetcher import FetchedPage, UnsafeSourceUrlError

ARTICLE_BODY = (
    b"<article><h1>Article title</h1><p>This article has meaningful main content "
    b"that can be imported into the user's knowledge repository.</p></article>"
)


def test_crawler_stays_same_origin_and_honors_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "https://docs.example.com/robots.txt": FetchedPage(
            "https://docs.example.com/robots.txt",
            "https://docs.example.com/robots.txt",
            "text/plain",
            b"User-agent: *\nAllow: /\n",
        ),
        "https://docs.example.com/": FetchedPage(
            "https://docs.example.com/",
            "https://docs.example.com/",
            "text/html",
            (
                ARTICLE_BODY + b'<a href="/guide/start">Guide</a>'
                b'<a href="/private">Private</a>'
                b'<a href="https://outside.example/guide">Outside</a>'
            ),
        ),
        "https://docs.example.com/guide/start": FetchedPage(
            "https://docs.example.com/guide/start",
            "https://docs.example.com/guide/start",
            "text/html",
            ARTICLE_BODY,
        ),
    }

    def fake_fetch(url: str, **kwargs: object) -> FetchedPage:
        return pages[url]

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch)
    result = crawl_site(
        "https://docs.example.com",
        CrawlPolicy(max_depth=1, max_pages=20, include_patterns=("/guide/*",)),
    )
    assert [page.final_url for page in result.pages] == [
        "https://docs.example.com/",
        "https://docs.example.com/guide/start",
    ]
    assert result.errors == ()


def test_default_url_import_reads_one_article_without_following_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def fake_fetch(url: str, **kwargs: object) -> FetchedPage:
        requested.append(url)
        return FetchedPage(
            url, url, "text/plain" if url.endswith("/robots.txt") else "text/html",
            b"User-agent: *\nAllow: /\n" if url.endswith("/robots.txt")
            else ARTICLE_BODY + b'<a href="/another">Another article</a>',
        )

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch)
    assert CrawlRequest(url="https://example.com").max_depth == 0
    assert CrawlRequest(url="https://example.com").max_pages == 1
    result = crawl_site("https://example.com", CrawlPolicy())
    assert len(result.pages) == 1
    assert len(requested) == 2
    assert result.pages[0].article.title == "Article title"


def test_browser_rendering_is_used_for_script_loaded_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crawler, "fetch_page", lambda url, **kwargs: FetchedPage(
        url, url, "text/plain" if url.endswith("/robots.txt") else "text/html",
        b"User-agent: *\nAllow: /\n" if url.endswith("/robots.txt") else b"<div id='app'></div>",
    ))
    rendered: list[str] = []

    def render(url: str) -> FetchedPage:
        rendered.append(url)
        return FetchedPage(url, url, "text/html", ARTICLE_BODY)

    result = crawl_site("https://example.com/article", CrawlPolicy(), render_page=render)
    assert rendered == ["https://example.com/article"]
    assert result.pages[0].acquisition == "browser"
    assert result.errors == ()


def test_verification_pages_are_reported_and_not_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(crawler, "fetch_page", lambda url, **kwargs: FetchedPage(
        url, url, "text/plain" if url.endswith("/robots.txt") else "text/html",
        b"User-agent: *\nAllow: /\n" if url.endswith("/robots.txt")
        else b"<title>Security check</title><p>Please verify you are human.</p>",
    ))
    result = crawl_site("https://example.com/article", CrawlPolicy())
    assert result.pages == ()
    assert result.errors[0].code == "verification_required"


def test_private_addresses_never_fall_back_to_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch(url: str, **kwargs: object) -> FetchedPage:
        if url.endswith("/robots.txt"):
            return FetchedPage(url, url, "text/plain", b"User-agent: *\nAllow: /\n")
        raise UnsafeSourceUrlError("private address")

    def forbidden_render(url: str) -> FetchedPage:
        raise AssertionError("browser fallback must not bypass public address validation")

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch)
    result = crawl_site("https://example.com", CrawlPolicy(), render_page=forbidden_render)
    assert result.pages == ()
    assert result.errors[0].code == "unsafe_url"


def test_crawl_page_limit_also_limits_failed_fetch_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested: list[str] = []

    def fake_fetch(url: str, **kwargs: object) -> FetchedPage:
        if url.endswith("/robots.txt"):
            return FetchedPage(url, url, "text/plain", b"User-agent: *\nAllow: /\n")
        requested.append(url)
        if url == "https://example.com/":
            links = b"".join(f'<a href="/page-{i}">Page</a>'.encode() for i in range(100))
            return FetchedPage(url, url, "text/html", ARTICLE_BODY + links)
        return FetchedPage(url, url, "text/html", b"<div id='app'></div>")

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch)
    result = crawl_site("https://example.com/", CrawlPolicy(max_depth=1, max_pages=3))
    assert len(requested) == 3
    assert len(result.pages) == 1
    assert len(result.errors) == 2
