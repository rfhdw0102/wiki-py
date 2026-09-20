from datetime import UTC, datetime
from pathlib import Path

import pytest
from wiki_agent.wiki.repository import StaleWikiRevisionError, WikiRepository
from wiki_agent.wiki.schema import Citation, PageKind, PageMetadata, WikiPage


def page(title: str, page_id: str) -> str:
    now = datetime.now(UTC)
    return WikiPage(
        metadata=PageMetadata(
            id=page_id,
            type=PageKind.SOURCE,
            title=title,
            created=now,
            updated=now,
            citations={"origin": Citation(source_revision_id="rev-1", span_id="all")},
        ),
        body=f"# {title}\n\nCompiled from source.[^origin]",
    ).to_markdown()


def test_publish_is_versioned_and_rejects_stale_base(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    result = repo.publish(
        {"sources/manual.md": page("Manual", "manual")},
        base_commit=base,
        author_name="Test User",
        author_email="test@example.com",
        message="Compile manual",
    )
    assert result.commit == repo.head()
    assert repo.read_page("sources/manual.md").metadata.title == "Manual"
    assert repo.history("sources/manual.md")[0]["subject"] == "Compile manual"
    with pytest.raises(StaleWikiRevisionError):
        repo.publish(
            {"sources/other.md": page("Other", "other")},
            base_commit=base,
            author_name="Test User",
            author_email="test@example.com",
            message="Stale draft",
        )


def test_schema_is_versioned_in_the_knowledge_repository(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    assert "Knowledge compilation rules" in repo.read_schema()
    result = repo.publish_schema(
        "# Updated rules\n\nEvery claim needs evidence.",
        base_commit=base,
        author_name="Admin",
        author_email="admin@example.com",
        message="Update compilation rules",
    )
    assert result.commit == repo.head()
    assert repo.read_schema().startswith("# Updated rules")
