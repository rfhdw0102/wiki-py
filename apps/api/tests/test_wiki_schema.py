from datetime import UTC, datetime

import pytest
from wiki_agent.wiki.schema import Citation, PageKind, PageMetadata, WikiPage


def metadata(kind: PageKind = PageKind.ENTITY) -> PageMetadata:
    now = datetime.now(UTC)
    return PageMetadata(
        id="compressor-3",
        type=kind,
        title="Compressor 3",
        created=now,
        updated=now,
        sources=["sources/manual.md"],
        citations={
            "speed": Citation(source_revision_id="rev-1", span_id="page-7"),
        },
    )


def test_wiki_page_round_trips() -> None:
    page = WikiPage(metadata=metadata(), body="Rated speed is 3000 rpm.[^speed]")
    assert WikiPage.from_markdown(page.to_markdown()) == page


def test_rejects_undeclared_citation() -> None:
    with pytest.raises(ValueError, match="undeclared citations"):
        WikiPage(metadata=metadata(), body="Unsupported.[^missing]")


def test_concept_requires_two_sources() -> None:
    with pytest.raises(ValueError, match="at least two"):
        WikiPage(metadata=metadata(PageKind.CONCEPT), body="A concept.[^speed]")


def test_rejects_dangerous_markdown() -> None:
    with pytest.raises(ValueError, match="unsafe HTML"):
        WikiPage(
            metadata=metadata(),
            body='<script>alert("unsafe")</script>[^speed]',
        )
