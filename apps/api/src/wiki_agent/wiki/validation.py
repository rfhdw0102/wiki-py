from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from wiki_agent.models import Source, SourceRevision, SourceSpan
from wiki_agent.wiki.schema import WikiPage


class CitationResolutionError(ValueError):
    pass


def validate_citation_targets(
    files: Mapping[str, str | None],
    db: Session,
    *,
    wiki_id: str,
) -> None:
    missing: list[str] = []
    mismatched: list[str] = []
    for path, content in files.items():
        if content is None:
            continue
        page = WikiPage.from_markdown(content)
        for key, citation in page.metadata.citations.items():
            span = db.scalar(
                select(SourceSpan)
                .join(
                    SourceRevision,
                    SourceRevision.id == SourceSpan.source_revision_id,
                )
                .join(Source, Source.id == SourceRevision.source_id)
                .where(
                    SourceSpan.id == citation.span_id,
                    Source.wiki_id == wiki_id,
                )
            )
            reference = f"{path}#{key}"
            if span is None:
                missing.append(f"{reference}->{citation.span_id}")
            elif span.source_revision_id != citation.source_revision_id:
                mismatched.append(reference)
    if missing:
        raise CitationResolutionError(
            "citations reference missing source spans: " + ", ".join(sorted(missing))
        )
    if mismatched:
        raise CitationResolutionError(
            "citations do not match their source revision: " + ", ".join(sorted(mismatched))
        )
