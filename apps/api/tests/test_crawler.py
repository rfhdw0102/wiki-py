import pytest
from wiki_agent.sources import crawler
from wiki_agent.sources.crawler import CrawlPolicy, crawl_site
from wiki_agent.sources.fetcher import FetchedPage


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
                b'<a href="/guide/start">Guide</a>'
                b'<a href="/private">Private</a>'
                b'<a href="https://outside.example/guide">Outside</a>'
            ),
        ),
        "https://docs.example.com/guide/start": FetchedPage(
            "https://docs.example.com/guide/start",
            "https://docs.example.com/guide/start",
            "text/html",
            b"<p>Start</p>",
        ),
    }

    def fake_fetch(url: str, **kwargs: object) -> FetchedPage:
        return pages[url]

    monkeypatch.setattr(crawler, "fetch_page", fake_fetch)
    result = crawl_site(
        "https://docs.example.com",
        CrawlPolicy(max_depth=1, include_patterns=("/guide/*",)),
    )
    assert [page.final_url for page in result.pages] == [
        "https://docs.example.com/",
        "https://docs.example.com/guide/start",
    ]
    assert result.errors == ()
