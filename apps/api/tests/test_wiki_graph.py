from datetime import UTC, datetime

from wiki_agent.api.wiki import wiki_graph
from wiki_agent.wiki.schema import PageKind, PageMetadata, WikiPage


def page(
    page_id: str,
    kind: PageKind,
    title: str,
    *,
    sources: list[str] | None = None,
    related: list[str] | None = None,
    body: str = "Knowledge.",
) -> WikiPage:
    now = datetime.now(UTC)
    return WikiPage(
        metadata=PageMetadata(
            id=page_id,
            type=kind,
            title=title,
            created=now,
            updated=now,
            sources=sources or [],
            related=related or [],
        ),
        body=body,
    )


class Repository:
    def __init__(self, pages: dict[str, WikiPage]) -> None:
        self.pages = pages

    def head(self) -> str:
        return "a" * 40

    def list_pages(self) -> list[str]:
        return list(self.pages)

    def read_page(self, path: str, revision: str) -> WikiPage:
        assert revision == self.head()
        return self.pages[path]


def test_graph_uses_wiki_page_titles_and_published_relationships() -> None:
    pages = {
        "sources/source.md": page("source", PageKind.SOURCE, "Original document"),
        "entities/cordis.md": page(
            "cordis",
            PageKind.ENTITY,
            "Cordis",
            sources=["sources/source.md"],
            related=["entities/agent.md"],
            body="Connected to [[entities/agent]].",
        ),
        "entities/agent.md": page(
            "agent", PageKind.ENTITY, "Agent", related=["entities/cordis.md"]
        ),
        "entities/isolated.md": page("isolated", PageKind.ENTITY, "Standalone"),
    }

    graph = wiki_graph(None, Repository(pages))

    assert graph["revision"] == "a" * 40
    assert graph["nodes"] == [
        {"path": path, "title": pages[path].metadata.title, "kind": pages[path].metadata.type}
        for path in pages
    ]
    assert graph["edges"] == [
        {"source": "entities/cordis.md", "target": "sources/source.md", "kind": "source"},
        {"source": "entities/cordis.md", "target": "entities/agent.md", "kind": "related"},
    ]


def test_graph_only_reads_the_selected_wiki_repository() -> None:
    first = Repository({"sources/first.md": page("first", PageKind.SOURCE, "First")})
    second = Repository({"sources/second.md": page("second", PageKind.SOURCE, "Second")})

    assert [node["title"] for node in wiki_graph(None, first)["nodes"]] == ["First"]
    assert [node["title"] for node in wiki_graph(None, second)["nodes"]] == ["Second"]
