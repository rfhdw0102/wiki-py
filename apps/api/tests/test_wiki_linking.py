from datetime import UTC, datetime
from pathlib import Path

import pytest
from wiki_agent.wiki.linking import complete_bidirectional_links
from wiki_agent.wiki.repository import WikiRepository, WikiRepositoryError
from wiki_agent.wiki.schema import Citation, PageKind, PageMetadata, WikiPage


def page(
    kind: PageKind, page_id: str, related: list[str] | None = None, body: str = "Content"
) -> str:
    now = datetime.now(UTC)
    return WikiPage(
        metadata=PageMetadata(
            id=page_id,
            type=kind,
            title=page_id,
            created=now,
            updated=now,
            related=related or [],
            citations={"fact": Citation(source_revision_id="revision", span_id="span")},
        ),
        body=body + "\n\nSupported fact.[^fact]",
    ).to_markdown()


def test_missing_reverse_source_link_is_reproduced_and_completed_before_validation(
    tmp_path: Path,
) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    source = "sources/revision.md"
    entity = "entities/agent-preset.md"
    files = {
        source: page(PageKind.SOURCE, "source-page"),
        entity: page(PageKind.ENTITY, "agent-preset", [source]),
    }
    with pytest.raises(WikiRepositoryError, match="bidirectional link"):
        repo.validate_changes(files, base_commit=base)
    linked = complete_bidirectional_links(files, {})
    repo.validate_changes(linked, base_commit=base)
    assert WikiPage.from_markdown(linked[source]).metadata.related == [entity]
    assert linked[entity] == files[entity]
    assert WikiPage.from_markdown(linked[source]).body == WikiPage.from_markdown(files[source]).body
    assert complete_bidirectional_links(linked, {}) == linked
    assert repo.head() == base
    assert repo.list_pages() == []


def test_existing_target_backlink_is_added_without_changing_body_sources_or_citations() -> None:
    old = page(PageKind.SOURCE, "old-source")
    new = page(PageKind.ENTITY, "new-entity", body="See [[sources/old-source]].")
    linked = complete_bidirectional_links(
        {"entities/new-entity.md": new},
        {"sources/old-source.md": old},
    )
    before = WikiPage.from_markdown(old)
    after = WikiPage.from_markdown(linked["sources/old-source.md"])
    assert after.metadata.related == ["entities/new-entity.md"]
    assert after.body == before.body
    assert after.metadata.citations == before.metadata.citations
    assert after.metadata.sources == before.metadata.sources
    assert after.metadata.created == before.metadata.created
    assert after.metadata.updated >= before.metadata.updated


def test_inbound_existing_links_are_preserved_but_unrelated_pages_are_not_rewritten() -> None:
    existing = {
        "sources/original.md": page(PageKind.SOURCE, "original", ["entities/topic.md"]),
        "entities/topic.md": page(PageKind.ENTITY, "topic", ["sources/original.md"]),
        "entities/unrelated.md": page(PageKind.ENTITY, "unrelated"),
    }
    files = {"entities/topic.md": page(PageKind.ENTITY, "topic", body="Updated supported content")}
    linked = complete_bidirectional_links(files, existing)
    assert WikiPage.from_markdown(linked["entities/topic.md"]).metadata.related == [
        "sources/original.md",
    ]
    assert "entities/unrelated.md" not in linked
    assert "sources/original.md" not in linked


def test_missing_or_deleted_targets_still_fail_validation_instead_of_being_invented(
    tmp_path: Path,
) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    files = {
        "entities/topic.md": page(PageKind.ENTITY, "topic", ["sources/missing.md"]),
        "sources/missing.md": None,
    }
    linked = complete_bidirectional_links(files, {})
    assert linked["sources/missing.md"] is None
    with pytest.raises(WikiRepositoryError, match="missing page"):
        repo.validate_changes(linked, base_commit=base)


def test_revision_page_listing_is_not_affected_by_new_working_tree_pages(tmp_path: Path) -> None:
    repo = WikiRepository(tmp_path / "knowledge")
    base = repo.initialize()
    path = repo.wiki_root / "sources" / "uncommitted.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page(PageKind.SOURCE, "uncommitted"), encoding="utf-8")
    assert repo.list_pages() == ["sources/uncommitted.md"]
    assert repo.list_pages(revision=base) == []
