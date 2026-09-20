import httpx
import pytest
from wiki_agent.llm.reranker import HttpReranker
from wiki_agent.search.fusion import FusedHit


def test_reranker_uses_returned_order(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())
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
