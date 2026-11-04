import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.db import Base
from wiki_agent.models import (
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    UserRole,
)
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
        user = User(
            email="user@qq.com",
            display_name="User",
            password_hash="unused",
            role=UserRole.USER,
        )
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Manuals")
        db.add(wiki)
        db.flush()
        source = Source(
            wiki_id=wiki.id,
            name="manual.txt",
            kind=SourceKind.TEXT,
            created_by_id=user.id,
        )
        db.add(source)
        db.flush()
        db.add(
            SourceRevision(
                id="revision-1",
                source_id=source.id,
                sha256="a" * 64,
                object_path="sources/aa/object",
                media_type="text/plain",
                size_bytes=10,
            )
        )
        db.commit()
        with pytest.raises(CitationResolutionError, match="missing source spans"):
            validate_citation_targets(
                {"sources/manual.md": PAGE},
                db,
                wiki_id=wiki.id,
            )
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
        validate_citation_targets(
            {"sources/manual.md": PAGE},
            db,
            wiki_id=wiki.id,
        )
