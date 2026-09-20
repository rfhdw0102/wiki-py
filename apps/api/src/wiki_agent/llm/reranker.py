from dataclasses import replace

import httpx

from wiki_agent.search.fusion import FusedHit


class RerankerResponseError(RuntimeError):
    pass


class HttpReranker:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        endpoint: str = "/rerank",
        timeout_seconds: float = 60,
    ) -> None:
        self.url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def rerank(self, query: str, hits: list[FusedHit], limit: int) -> list[FusedHit]:
        if not hits:
            return []
        response = httpx.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "query": query,
                "documents": [hit.content for hit in hits],
                "top_n": min(limit, len(hits)),
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results")
        if not isinstance(results, list):
            raise RerankerResponseError("reranker response has no results array")
        reranked: list[FusedHit] = []
        seen: set[int] = set()
        for item in results:
            if not isinstance(item, dict):
                raise RerankerResponseError("reranker result must be an object")
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index >= len(hits)
                or index in seen
            ):
                raise RerankerResponseError("reranker returned an invalid document index")
            if isinstance(score, bool) or not isinstance(score, int | float):
                raise RerankerResponseError("reranker returned an invalid relevance score")
            seen.add(index)
            reranked.append(replace(hits[index], score=float(score)))
        return reranked
