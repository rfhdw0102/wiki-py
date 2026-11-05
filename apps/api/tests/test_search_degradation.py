import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.api import wiki as wiki_api
from wiki_agent.api.schemas import WikiSearchRequest
from wiki_agent.db import Base
from wiki_agent.llm.reranker import RerankerResponseError
from wiki_agent.search.fusion import FusedHit


class FailingEmbeddings:
    def embed_query(self, query: str) -> list[float]:
        raise httpx.ConnectError("embedding service offline")


class FailingReranker:
    def rerank(self, query: str, hits: list[FusedHit], limit: int) -> list[FusedHit]:
        raise RerankerResponseError("invalid response")


class FailingModelFactory:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def assigned_profile_id(self, purpose: str) -> str | None:
        return purpose if purpose in {"embedding", "reranker"} else None

    def embeddings(self, profile_id: str) -> FailingEmbeddings:
        return FailingEmbeddings()

    def reranker(self, profile_id: str) -> FailingReranker:
        return FailingReranker()


def test_search_degrades_when_embedding_and_reranker_are_unavailable(
    monkeypatch,
) -> None:
    hits = [
        FusedHit(
            f"concepts/safety-{index}.md",
            1.0 - index / 100,
            ("bm25",),
            "Safety",
            {},
        )
        for index in range(8)
    ]
    monkeypatch.setattr(wiki_api, "ModelFactory", FailingModelFactory)
    monkeypatch.setattr(wiki_api, "hybrid_search", lambda *args, **kwargs: hits)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        result = wiki_api._execute_search(
            WikiSearchRequest(query="safety"),
            db,
            "user-id",
            "wiki-id",
        )

    assert [hit.document_id for hit in result.hits] == [hit.document_id for hit in hits]
    assert result.channels == ["bm25", "graph"]
    assert [item["channel"] for item in result.degradations] == [
        "vector",
        "reranker",
    ]


def test_reranker_is_skipped_for_small_corroborated_result_sets() -> None:
    hits = [
        FusedHit("one", 0.03, ("bm25", "graph"), "One", {}),
        FusedHit("two", 0.02, ("bm25",), "Two", {}),
        FusedHit("three", 0.01, ("graph",), "Three", {}),
    ]

    assert wiki_api._should_rerank(hits, fallback_used=False) is False
    assert wiki_api._should_rerank(hits, fallback_used=True) is True
