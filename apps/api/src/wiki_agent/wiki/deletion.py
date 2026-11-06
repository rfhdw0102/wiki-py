import re
from collections.abc import Mapping
from datetime import UTC, datetime

from wiki_agent.wiki.schema import CITATION_PATTERN, WIKI_LINK_PATTERN, PageKind, WikiPage


def remove_source_references(
    files: Mapping[str, str],
    revision_ids: set[str],
    *,
    removed_paths: frozenset[str] = frozenset(),
) -> dict[str, str | None]:
    pages = {path: WikiPage.from_markdown(content) for path, content in files.items()}
    source_paths = {f"sources/{revision_id}.md" for revision_id in revision_ids}
    deleted = set(removed_paths) | source_paths
    for path, page in pages.items():
        support = set(page.metadata.sources) | {
            f"sources/{citation.source_revision_id}.md"
            for citation in page.metadata.citations.values()
        } | {
            f"{link}.md" for link in WIKI_LINK_PATTERN.findall(page.body)
            if link.startswith("sources/")
        }
        if page.metadata.type is PageKind.SOURCE:
            support.add(path)
        if support and support <= source_paths:
            deleted.add(path)
        elif support - source_paths and path not in source_paths:
            deleted.discard(path)

    changes: dict[str, str | None] = {}
    for path, page in pages.items():
        if path in deleted:
            changes[path] = None
            continue
        removed_keys = {
            key for key, citation in page.metadata.citations.items()
            if citation.source_revision_id in revision_ids
        }
        body = _remove_cited_content(
            page.body, removed_keys, preserve_content=page.metadata.type is PageKind.CONCEPT,
        )
        body = WIKI_LINK_PATTERN.sub(
            lambda match: "" if f"{match[1]}.md" in deleted else match[0],
            body,
        ).strip()
        sources = [item for item in page.metadata.sources if item not in deleted]
        related = [item for item in page.metadata.related if item not in deleted]
        used_keys = set(CITATION_PATTERN.findall(body))
        citations = {
            key: citation for key, citation in page.metadata.citations.items()
            if key not in removed_keys and key in used_keys
        }
        if (
            body == page.body and sources == page.metadata.sources
            and related == page.metadata.related and citations == page.metadata.citations
        ):
            continue
        page.body = body or f"# {page.metadata.title}"
        page.metadata.sources = sources
        page.metadata.related = related
        page.metadata.citations = citations
        now = datetime.now(UTC)
        updated = now if page.metadata.updated.tzinfo else now.replace(tzinfo=None)
        page.metadata.updated = max(page.metadata.updated, updated)
        content = page.to_markdown()
        WikiPage.from_markdown(content)
        changes[path] = content
    return changes


def _remove_cited_content(
    body: str, removed_keys: set[str], *, preserve_content: bool,
) -> str:
    if not removed_keys:
        return body
    for key in removed_keys:
        body = re.sub(
            rf"(?m)^\[\^{re.escape(key)}\]:[^\n]*(?:\n(?:[ \t]{{2,}}|\t)[^\n]*)*",
            "", body,
        )
    blocks = re.split(r"(\n[ \t]*\n)", body)
    for index, block in enumerate(blocks):
        if preserve_content:
            continue
        keys = set(CITATION_PATTERN.findall(block))
        if keys and keys <= removed_keys:
            blocks[index] = ""
        elif re.search(r"(?m)^[ \t]*(?:[-*+] |\d+[.)] |\|)", block):
            blocks[index] = "\n".join(
                line for line in block.split("\n")
                if not (
                    (line_keys := set(CITATION_PATTERN.findall(line)))
                    and line_keys <= removed_keys
                )
            )
    return CITATION_PATTERN.sub(
        lambda match: "" if match[1] in removed_keys else match[0],
        "".join(blocks),
    ).strip()
