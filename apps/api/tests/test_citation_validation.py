import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.db import Base
from wiki_agent.models import SourceSpan
from wiki_agent.wiki.validation import CitationResolutionError, validate_citation_targets

PAGE = """---
id: manual
type: source
title: Manual
created: 2026-01-01T00:00:00Z
updated: 2026-01-01T00:00:00Z
citations:
  origin:
    source_revision_id: revision-1
    span_id: revision-1:0
---

# Manual

Safe operation.[^origin]
"""


def test_citation_target_must_exist() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        with pytest.raises(CitationResolutionError, match="missing source spans"):
            validate_citation_targets({"sources/manual.md": PAGE}, db)
        db.add(
            SourceSpan(
                id="revision-1:0",
                source_revision_id="revision-1",
                ordinal=0,
                content="Safe operation.",
                locator={"line": 1},
            )
        )
        db.commit()
        validate_citation_targets({"sources/manual.md": PAGE}, db)
