from collections.abc import Mapping
from datetime import UTC, datetime

from wiki_agent.wiki.schema import WikiPage


def complete_bidirectional_links(
    files: Mapping[str, str | None],
    existing_pages: Mapping[str, str],
) -> dict[str, str | None]:
    combined: dict[str, str | None] = {**existing_pages, **files}
    pages = {
        path: WikiPage.from_markdown(content)
        for path, content in combined.items()
        if content is not None
    }
    changed = set(files)
    repaired: set[str] = set()
    for path, page in sorted(pages.items()):
        for link in sorted(page.links):
            target = link if link.endswith(".md") else f"{link}.md"
            if path not in changed and target not in changed:
                continue
            related = pages.get(target)
            if related is None or path == target:
                continue
            reverse_links = {
                item if item.endswith(".md") else f"{item}.md" for item in related.links
            }
            if path not in reverse_links:
                related.metadata.related = sorted({*related.metadata.related, path})
                repaired.add(target)
    result = dict(files)
    now = datetime.now(UTC)
    for path in sorted(repaired):
        page = pages[path]
        updated = now if page.metadata.updated.tzinfo else now.replace(tzinfo=None)
        page.metadata.updated = max(page.metadata.updated, updated)
        result[path] = page.to_markdown()
    return result
