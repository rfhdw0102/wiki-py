import httpx
import pytest
from wiki_agent.llm.reranker import HttpReranker, RerankerRequestError
from wiki_agent.search.fusion import FusedHit


def test_reranker_uses_returned_order(monkeypatch: pytest.MonkeyPatch) -> None:
    request_headers: dict[str, str] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.5},
                ]
            }

    def post(*args, **kwargs):
        request_headers.update(kwargs["headers"])
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    hits = [
        FusedHit("first", 0.1, ("bm25",), "first", {}),
        FusedHit("second", 0.2, ("vector",), "second", {}),
    ]
    result = HttpReranker(
        base_url="http://reranker.test",
        api_key="secret",
        model="reranker",
    ).rerank("query", hits, 2)
    assert [hit.document_id for hit in result] == ["second", "first"]
    assert result[0].score == 0.9
    assert request_headers["Authorization"] == "Bearer secret"


def test_reranker_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "post", fail)
    reranker = HttpReranker(
        base_url="http://reranker.test",
        api_key="secret",
        model="reranker",
    )

    with pytest.raises(RerankerRequestError, match="request failed"):
        reranker.rerank(
            "query",
            [FusedHit("first", 0.1, ("bm25",), "first", {})],
            1,
        )
