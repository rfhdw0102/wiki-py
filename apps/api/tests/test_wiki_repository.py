from datetime import UTC, datetime
from pathlib import Path

import pytest
from wiki_agent.wiki.repository import (
    StaleWikiRevisionError,
    WikiRepository,
    WikiRepositoryError,
)
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
    assert "知识编译规则" in repo.read_schema()
    result = repo.publish_schema(
        "# Updated rules\n\nEvery claim needs evidence.",
        base_commit=base,
        author_name="Admin",
        author_email="admin@example.com",
        message="Update compilation rules",
    )
    assert result.commit == repo.head()
    assert repo.read_schema().startswith("# Updated rules")


def test_schema_reader_returns_the_effective_working_tree_content(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    repo.initialize()
    working_schema = repo.root / "schema" / "AGENTS.md"
    working_schema.write_text("# 中文编译规则\n\n保留来源引用。", encoding="utf-8")

    assert repo.read_schema() == "# 中文编译规则\n\n保留来源引用。"


def test_page_source_references_must_resolve_to_source_pages(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    now = datetime.now(UTC)
    entity = WikiPage(
        metadata=PageMetadata(
            id="entity-one",
            type=PageKind.ENTITY,
            title="Entity One",
            created=now,
            updated=now,
            sources=["sources/missing.md"],
        ),
        body="# Entity One",
    ).to_markdown()

    with pytest.raises(WikiRepositoryError, match="missing source page"):
        repo.validate_changes({"entities/entity-one.md": entity}, base_commit=base)


def test_new_concept_still_requires_two_sources(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    now = datetime.now(UTC)
    concept = WikiPage.model_construct(
        metadata=PageMetadata(
            id="new-concept", type=PageKind.CONCEPT, title="New concept",
            created=now, updated=now, sources=["sources/manual.md"],
        ),
        body="A concept.",
    ).to_markdown()
    files = {
        "sources/manual.md": page("Manual", "manual"),
        "concepts/new-concept.md": concept,
    }
    with pytest.raises(WikiRepositoryError, match="at least two"):
        repo.validate_changes(files, base_commit=base)
    with pytest.raises(WikiRepositoryError, match="at least two"):
        repo.publish(
            files, base_commit=base, author_name="Owner",
            author_email="owner@localhost", message="Invalid new concept",
        )
    assert repo.head() == base
    assert repo.list_pages() == []
