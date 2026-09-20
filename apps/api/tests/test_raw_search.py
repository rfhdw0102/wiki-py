from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.db import Base
from wiki_agent.models import SourceSpan
from wiki_agent.search.repository import RawSearchRepository, raw_hybrid_search


def test_raw_search_is_explicitly_labeled_as_fallback() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
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
        hits = raw_hybrid_search(RawSearchRepository(db), "isolation valve")
    assert hits[0].document_id == "raw:revision-1:0"
    assert hits[0].channels == ("raw_bm25",)
    assert hits[0].metadata["layer"] == "raw"
