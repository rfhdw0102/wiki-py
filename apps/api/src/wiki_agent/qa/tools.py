import logging
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from openai import OpenAIError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from wiki_agent.config import Settings
from wiki_agent.llm.factory import ModelFactory
from wiki_agent.models import (
    FeatureFlag,
    KnowledgeNode,
    Source,
    SourceRevision,
    SourceSpan,
    WikiDocument,
    WikiIndexState,
)
from wiki_agent.search.fusion import FusedHit, reciprocal_rank_fusion
from wiki_agent.search.repository import (
    RawSearchRepository,
    WikiSearchRepository,
    hybrid_search,
    raw_hybrid_search,
)

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 8
SEARCH_EXCERPT = 900
READ_LIMIT = 8_000
EVIDENCE_LIMIT = 40_000


class SearchWiki(BaseModel):
    """Find relevant pages in this Wiki using lexical, graph and optional semantic retrieval."""

    query: str = Field(min_length=1, max_length=1000)


class ReadWikiPage(BaseModel):
    """Read a single Wiki page by a path returned from search_wiki or expand_graph."""

    page_id: str = Field(min_length=1, max_length=1024)


class ExpandGraph(BaseModel):
    """Explore Wiki pages linked to an existing knowledge node ID."""

    node_id: str = Field(min_length=1, max_length=128)
    depth: int = Field(default=2, ge=1, le=2)


class SearchRaw(BaseModel):
    """Search original source fragments when Wiki pages lack necessary detail."""

    query: str = Field(min_length=1, max_length=1000)


class ReadSourceSpan(BaseModel):
    """Read a source fragment by a raw:... ID returned from search_raw."""

    span_id: str = Field(min_length=1, max_length=196)


TOOL_SCHEMAS: tuple[type[BaseModel], ...] = (
    SearchWiki,
    ReadWikiPage,
    ExpandGraph,
    SearchRaw,
    ReadSourceSpan,
)


class KnowledgeToolError(ValueError):
    pass


class KnowledgeTools:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        user_id: str,
        wiki_id: str,
        *,
        on_degradation: Callable[[dict[str, str]], None] | None = None,
        embedding_profile_id: str | None = None,
        reranker_profile_id: str | None = None,
        allow_raw: bool = True,
        limit: int = 10,
        entry_node_ids: Sequence[str] = (),
    ) -> None:
        self.db = db
        self.settings = settings
        self.user_id = user_id
        self.wiki_id = wiki_id
        self.factory = ModelFactory(db, settings, user_id)
        self.evidence: dict[str, FusedHit] = {}
        self.on_degradation = on_degradation
        self.embedding_profile_id = embedding_profile_id
        self.reranker_profile_id = reranker_profile_id
        self.allow_raw = allow_raw
        self.result_limit = min(limit, SEARCH_LIMIT)
        self.entry_node_ids = tuple(entry_node_ids)

    def call(self, name: str, args: dict[str, Any]) -> dict[str, object]:
        methods: dict[str, tuple[type[BaseModel], Callable[[BaseModel], dict[str, object]]]] = {
            "SearchWiki": (SearchWiki, self._search_wiki),
            "ReadWikiPage": (ReadWikiPage, self._read_wiki_page),
            "ExpandGraph": (ExpandGraph, self._expand_graph),
            "SearchRaw": (SearchRaw, self._search_raw),
            "ReadSourceSpan": (ReadSourceSpan, self._read_source_span),
        }
        if name not in methods:
            raise KnowledgeToolError(f"Unsupported knowledge tool: {name}")
        schema, method = methods[name]
        return method(schema.model_validate(args))

    def _vector(self, query: str) -> tuple[list[float] | None, str | None]:
        profile_id = self.embedding_profile_id or self.factory.assigned_profile_id("embedding")
        if profile_id is None:
            return None, None
        try:
            return self.factory.embeddings(profile_id).embed_query(query), profile_id
        except (OpenAIError, httpx.HTTPError) as exc:
            logger.warning(
                "Embedding unavailable for knowledge tool",
                exc_info=exc,
                extra={"wiki_id": self.wiki_id, "user_id": self.user_id},
            )
            if self.on_degradation is not None:
                self.on_degradation(
                    {"channel": "vector", "reason": "Embedding unavailable; using lexical search."}
                )
            return None, profile_id

    def _record(self, hits: list[FusedHit], *, layer: str, full: bool = False) -> dict[str, object]:
        entries: list[dict[str, object]] = []
        for hit in hits[: self.result_limit]:
            content = hit.content[: READ_LIMIT if full else SEARCH_EXCERPT]
            known = self.evidence.get(hit.document_id)
            self.evidence[hit.document_id] = FusedHit(
                hit.document_id,
                max(known.score, hit.score) if known is not None else hit.score,
                tuple(sorted({*known.channels, *hit.channels}))
                if known is not None
                else hit.channels,
                known.content
                if known is not None and len(known.content) >= len(content)
                else content,
                {**(known.metadata if known is not None else {}), **hit.metadata, "layer": layer},
            )
            entries.append(
                {
                    "document_id": hit.document_id,
                    "content": content,
                    "truncated": len(content) < len(hit.content),
                    "channels": hit.channels,
                    "metadata": {**hit.metadata, "layer": layer},
                }
            )
        return {"results": entries}

    def _search_wiki(self, data: BaseModel) -> dict[str, object]:
        assert isinstance(data, SearchWiki)
        vector, profile_id = self._vector(data.query)
        state = self.db.get(WikiIndexState, (self.wiki_id, "published"))
        if state is None or state.embedding_profile_id != profile_id:
            vector = None
        hits = hybrid_search(
            WikiSearchRepository(self.db, self.wiki_id),
            data.query,
            embedding=vector,
            entry_node_ids=self.entry_node_ids,
            limit=self.result_limit,
        )
        return self._record(hits, layer="wiki")

    def _read_wiki_page(self, data: BaseModel) -> dict[str, object]:
        assert isinstance(data, ReadWikiPage)
        page = self.db.scalar(
            select(WikiDocument).where(
                WikiDocument.wiki_id == self.wiki_id,
                WikiDocument.path == data.page_id,
            )
        )
        if page is None:
            raise KnowledgeToolError("Wiki page not found in the current Wiki")
        return self._record(
            [FusedHit(page.path, 1.0, ("read",), page.content, page.metadata_json)],
            layer="wiki",
            full=True,
        )

    def _expand_graph(self, data: BaseModel) -> dict[str, object]:
        assert isinstance(data, ExpandGraph)
        node = self.db.scalar(
            select(KnowledgeNode.id).where(
                KnowledgeNode.wiki_id == self.wiki_id,
                KnowledgeNode.id == data.node_id,
            )
        )
        if node is None:
            raise KnowledgeToolError("Knowledge node not found in the current Wiki")
        graph_hits = WikiSearchRepository(self.db, self.wiki_id).graph(
            [node], max_depth=data.depth
        )[: self.result_limit]
        return self._record(
            reciprocal_rank_fusion([graph_hits], limit=self.result_limit), layer="wiki"
        )

    def _raw_enabled(self) -> bool:
        flag = self.db.get(FeatureFlag, "raw_rag")
        return bool(
            self.allow_raw and self.settings.raw_rag_enabled and flag is not None and flag.enabled
        )

    def _search_raw(self, data: BaseModel) -> dict[str, object]:
        assert isinstance(data, SearchRaw)
        if not self._raw_enabled():
            raise KnowledgeToolError("Raw retrieval is disabled")
        vector, profile_id = self._vector(data.query)
        hits = raw_hybrid_search(
            RawSearchRepository(self.db, self.wiki_id),
            data.query,
            embedding=vector,
            embedding_profile_id=profile_id,
            limit=self.result_limit,
        )
        return self._record(hits, layer="raw")

    def _read_source_span(self, data: BaseModel) -> dict[str, object]:
        assert isinstance(data, ReadSourceSpan)
        if not self._raw_enabled():
            raise KnowledgeToolError("Raw retrieval is disabled")
        span_id = data.span_id.removeprefix("raw:")
        row = self.db.execute(
            select(SourceSpan, SourceRevision, Source)
            .join(SourceRevision, SourceRevision.id == SourceSpan.source_revision_id)
            .join(Source, Source.id == SourceRevision.source_id)
            .where(Source.wiki_id == self.wiki_id, SourceSpan.id == span_id)
        ).first()
        if row is None:
            raise KnowledgeToolError("Source fragment not found in the current Wiki")
        span, revision, source = row
        hit = FusedHit(
            f"raw:{span.id}",
            1.0,
            ("read",),
            span.content,
            {
                "layer": "raw",
                "span_id": span.id,
                "source_revision_id": revision.id,
                "source_name": source.name,
                "heading": span.heading,
                "locator": span.locator,
            },
        )
        return self._record([hit], layer="raw", full=True)

    def final_evidence(self, *, limit: int = 10) -> list[FusedHit]:
        results: list[FusedHit] = []
        size = 0
        for hit in sorted(self.evidence.values(), key=lambda item: (-item.score, item.document_id)):
            if size + len(hit.content) > EVIDENCE_LIMIT:
                continue
            results.append(hit)
            size += len(hit.content)
            if len(results) == min(limit, self.result_limit):
                break
        return results

    def snapshot_evidence(self) -> list[FusedHit]:
        return list(self.evidence.values())

    def restore_evidence(self, evidence: list[FusedHit]) -> None:
        self.evidence = {hit.document_id: hit for hit in evidence}
