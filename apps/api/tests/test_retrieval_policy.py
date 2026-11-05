from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.api import wiki as wiki_api
from wiki_agent.api.schemas import WikiSearchRequest
from wiki_agent.db import Base
from wiki_agent.llm.query_rewriter import QueryPlan
from wiki_agent.models import (
    FeatureFlag,
    PersonalWiki,
    User,
    UserRole,
    WikiIndexState,
)
from wiki_agent.search.fusion import FusedHit


class NoModelFactory:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def assigned_profile_id(self, purpose: str) -> None:
        return None


def test_wiki_quality_requires_three_hits_and_cross_channel_top_result() -> None:
    single_channel_hits = [
        FusedHit("one", 0.03, ("bm25",), "One", {}),
        FusedHit("two", 0.02, ("graph",), "Two", {}),
        FusedHit("three", 0.01, ("vector",), "Three", {}),
    ]
    corroborated_hits = [
        FusedHit("one", 0.03, ("bm25", "graph"), "One", {}),
        *single_channel_hits[1:],
    ]

    weak = wiki_api._evaluate_wiki_quality(
        single_channel_hits,
        minimum_hits=3,
        require_cross_channel_top=True,
    )
    strong = wiki_api._evaluate_wiki_quality(
        corroborated_hits,
        minimum_hits=3,
        require_cross_channel_top=True,
    )

    assert weak["sufficient"] is False
    assert strong["sufficient"] is True


def test_weak_wiki_results_are_preserved_when_raw_evidence_is_added(
    monkeypatch,
) -> None:
    wiki_hits = [
        FusedHit("wiki-one", 0.02, ("bm25",), "Wiki one", {}),
        FusedHit("wiki-two", 0.01, ("graph",), "Wiki two", {}),
    ]
    raw_hits = [FusedHit("raw:one", 0.03, ("raw_bm25",), "Raw one", {"layer": "raw"})]
    monkeypatch.setattr(wiki_api, "ModelFactory", NoModelFactory)
    monkeypatch.setattr(wiki_api, "hybrid_search", lambda *args, **kwargs: wiki_hits)
    monkeypatch.setattr(wiki_api, "raw_hybrid_search", lambda *args, **kwargs: raw_hits)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(FeatureFlag(key="raw_rag", enabled=True))
        db.commit()

        result = wiki_api._execute_search(
            WikiSearchRequest(query="bearing heat"),
            db,
            "user-id",
            "wiki-id",
        )

    assert result.fallback_used is True
    assert result.layer == "hybrid"
    assert result.wiki_quality["sufficient"] is False
    assert {hit.document_id for hit in result.hits} == {
        "wiki-one",
        "wiki-two",
        "raw:one",
    }


class CapturingEmbeddings:
    query: str | None = None

    def embed_query(self, query: str) -> list[float]:
        self.query = query
        return [1.0, 0.0]


class RewriteModelFactory:
    embeddings_client = CapturingEmbeddings()

    def __init__(self, *args, **kwargs) -> None:
        pass

    def assigned_profile_id(self, purpose: str) -> str | None:
        if purpose in {"answer", "embedding"}:
            return purpose
        return None

    def chat(self, profile_id: str) -> object:
        assert profile_id == "answer"
        return object()

    def embeddings(self, profile_id: str) -> CapturingEmbeddings:
        assert profile_id == "embedding"
        return self.embeddings_client


class QueryRewriter:
    def __init__(self, model: object) -> None:
        pass

    def rewrite(self, question: str) -> QueryPlan:
        return QueryPlan(
            lexical_query="A-103 bearing temperature",
            semantic_query="How to handle an overheated A-103 bearing",
        )


def test_query_rewrite_routes_lexical_and_semantic_queries_separately(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def hybrid_search(*args, **kwargs):
        captured["lexical_query"] = args[1]
        captured["embedding"] = kwargs["embedding"]
        return [
            FusedHit("one", 0.03, ("bm25", "vector"), "One", {}),
            FusedHit("two", 0.02, ("bm25",), "Two", {}),
            FusedHit("three", 0.01, ("vector",), "Three", {}),
        ]

    monkeypatch.setattr(wiki_api, "ModelFactory", RewriteModelFactory)
    monkeypatch.setattr(wiki_api, "WikiQueryRewriter", QueryRewriter)
    monkeypatch.setattr(wiki_api, "hybrid_search", hybrid_search)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            email="owner@qq.com",
            display_name="Owner",
            password_hash="unused",
            role=UserRole.USER,
        )
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Knowledge")
        db.add(wiki)
        db.flush()
        db.add(
            WikiIndexState(
                wiki_id=wiki.id,
                name="published",
                commit_sha="a" * 40,
                embedding_profile_id="embedding",
            )
        )
        db.commit()

        result = wiki_api._execute_search(
            WikiSearchRequest(query="三号设备转动的地方很烫怎么办"),
            db,
            user.id,
            wiki.id,
        )

    assert captured["lexical_query"] == "A-103 bearing temperature"
    assert captured["embedding"] == [1.0, 0.0]
    assert RewriteModelFactory.embeddings_client.query == (
        "How to handle an overheated A-103 bearing"
    )
    assert result.query_plan.lexical_query == "A-103 bearing temperature"
    assert result.wiki_quality["sufficient"] is True
