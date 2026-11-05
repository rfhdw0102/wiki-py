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
from wiki_agent.search.repository import RawSearchRepository, raw_hybrid_search


def test_raw_search_is_explicitly_labeled_as_fallback() -> None:
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
        revision = SourceRevision(
            id="revision-1",
            source_id=source.id,
            sha256="a" * 64,
            object_path="sources/aa/object",
            media_type="text/plain",
            size_bytes=10,
        )
        db.add(revision)
        db.add(
            SourceSpan(
                id="revision-1:0",
                source_revision_id="revision-1",
                ordinal=0,
                heading="Shutdown",
                content="Close the isolation valve before shutdown.",
                locator={"page": 4},
            )
        )
        db.commit()
        hits = raw_hybrid_search(
            RawSearchRepository(db, wiki.id),
            "isolation valve",
        )
    assert hits[0].document_id == "raw:revision-1:0"
    assert hits[0].channels == ("raw_bm25",)
    assert hits[0].metadata["layer"] == "raw"
