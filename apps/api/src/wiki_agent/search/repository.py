import json
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from wiki_agent.search.fusion import FusedHit, RankedHit, reciprocal_rank_fusion


class WikiSearchRepository:
    def __init__(self, db: Session, wiki_id: str) -> None:
        self.db = db
        self.wiki_id = wiki_id

    def bm25(self, query: str, limit: int = 20) -> list[RankedHit]:
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            statement = text(
                """
                SELECT path, content, metadata_json, paradedb.score(id) AS score
                FROM wiki_documents
                WHERE wiki_id = :wiki_id
                  AND (title @@@ :query OR content @@@ :query)
                ORDER BY score DESC
                LIMIT :limit
                """
            )
        else:
            statement = text(
                """
                SELECT path, content, metadata_json, 1.0 AS score
                FROM wiki_documents
                WHERE wiki_id = :wiki_id
                  AND (lower(title) LIKE lower(:pattern)
                   OR lower(content) LIKE lower(:pattern))
                ORDER BY title
                LIMIT :limit
                """
            )
        parameters: dict[str, object] = {
            "wiki_id": self.wiki_id,
            "query": query,
            "limit": limit,
        }
        if self.db.bind is None or self.db.bind.dialect.name != "postgresql":
            parameters["pattern"] = f"%{query}%"
        rows = self.db.execute(statement, parameters).mappings()
        return [
            RankedHit(
                document_id=row["path"],
                channel="bm25",
                rank=index,
                source_score=float(row["score"]),
                content=row["content"],
                metadata=_as_dict(row["metadata_json"]),
            )
            for index, row in enumerate(rows, start=1)
        ]

    def vector(self, embedding: Sequence[float], limit: int = 20) -> list[RankedHit]:
        if not embedding:
            return []
        if self.db.bind is None or self.db.bind.dialect.name != "postgresql":
            return []
        rows = self.db.execute(
            text(
                """
                SELECT path, content, metadata_json,
                       1 - (embedding <=> CAST(:embedding AS vector)) AS score
                FROM wiki_documents
                WHERE wiki_id = :wiki_id
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
                """
            ),
            {
                "wiki_id": self.wiki_id,
                "embedding": json.dumps(list(embedding)),
                "limit": limit,
            },
        ).mappings()
        return [
            RankedHit(
                document_id=row["path"],
                channel="vector",
                rank=index,
                source_score=float(row["score"]),
                content=row["content"],
                metadata=_as_dict(row["metadata_json"]),
            )
            for index, row in enumerate(rows, start=1)
        ]

    def graph(self, entry_node_ids: Sequence[str], max_depth: int = 2) -> list[RankedHit]:
        if not entry_node_ids:
            return []
        if max_depth < 1 or max_depth > 5:
            raise ValueError("max_depth must be between 1 and 5")
        placeholders = ", ".join(f":node_{index}" for index in range(len(entry_node_ids)))
        parameters: dict[str, object] = {
            f"node_{index}": node_id for index, node_id in enumerate(entry_node_ids)
        }
        parameters["wiki_id"] = self.wiki_id
        parameters["max_depth"] = max_depth
        rows = self.db.execute(
            text(
                f"""
                WITH RECURSIVE traversal(node_id, depth) AS (
                    SELECT record_id, 0
                    FROM knowledge_nodes
                    WHERE wiki_id = :wiki_id AND id IN ({placeholders})
                    UNION
                    SELECT edge.target_node_id, traversal.depth + 1
                    FROM traversal
                    JOIN knowledge_edges edge
                      ON edge.source_node_id = traversal.node_id
                     AND edge.wiki_id = :wiki_id
                    WHERE traversal.depth < :max_depth
                )
                SELECT document.path, document.content, document.metadata_json, reached.depth
                FROM (
                    SELECT node_id, MIN(depth) AS depth
                    FROM traversal
                    GROUP BY node_id
                ) reached
                JOIN knowledge_nodes node ON node.record_id = reached.node_id
                JOIN wiki_documents document
                  ON document.wiki_id = :wiki_id
                 AND document.page_id = node.id
                ORDER BY reached.depth, document.path
                """
            ),
            parameters,
        ).mappings()
        return [
            RankedHit(
                document_id=row["path"],
                channel="graph",
                rank=index,
                source_score=1.0 / (int(row["depth"]) + 1),
                content=row["content"],
                metadata=_as_dict(row["metadata_json"]),
            )
            for index, row in enumerate(rows, start=1)
        ]


class RawSearchRepository:
    def __init__(self, db: Session, wiki_id: str) -> None:
        self.db = db
        self.wiki_id = wiki_id

    def bm25(self, query: str, limit: int = 20) -> list[RankedHit]:
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            statement = text(
                """
                SELECT span.id, span.source_revision_id, span.heading,
                       span.content, span.locator, paradedb.score(span.id) AS score
                FROM source_spans AS span
                JOIN source_revisions AS revision ON revision.id = span.source_revision_id
                JOIN sources AS source ON source.id = revision.source_id
                WHERE source.wiki_id = :wiki_id
                  AND (span.heading @@@ :query OR span.content @@@ :query)
                ORDER BY score DESC
                LIMIT :limit
                """
            )
            parameters: dict[str, object] = {"query": query, "limit": limit}
        else:
            statement = text(
                """
                SELECT span.id, span.source_revision_id, span.heading,
                       span.content, span.locator, 1.0 AS score
                FROM source_spans AS span
                JOIN source_revisions AS revision ON revision.id = span.source_revision_id
                JOIN sources AS source ON source.id = revision.source_id
                WHERE source.wiki_id = :wiki_id
                  AND (lower(COALESCE(span.heading, '')) LIKE lower(:pattern)
                   OR lower(span.content) LIKE lower(:pattern))
                ORDER BY span.id
                LIMIT :limit
                """
            )
            parameters = {
                "wiki_id": self.wiki_id,
                "pattern": f"%{query}%",
                "limit": limit,
            }
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            parameters["wiki_id"] = self.wiki_id
        rows = self.db.execute(statement, parameters).mappings()
        return [
            RankedHit(
                document_id=f"raw:{row['id']}",
                channel="raw_bm25",
                rank=index,
                source_score=float(row["score"]),
                content=row["content"],
                metadata={
                    "layer": "raw",
                    "span_id": row["id"],
                    "source_revision_id": row["source_revision_id"],
                    "heading": row["heading"],
                    "locator": _as_dict(row["locator"]),
                },
            )
            for index, row in enumerate(rows, start=1)
        ]

    def vector(
        self,
        embedding: Sequence[float],
        embedding_profile_id: str,
        limit: int = 20,
    ) -> list[RankedHit]:
        if not embedding or self.db.bind is None or self.db.bind.dialect.name != "postgresql":
            return []
        rows = self.db.execute(
            text(
                """
                SELECT span.id, span.source_revision_id, span.heading,
                       span.content, span.locator,
                       1 - (span.embedding <=> CAST(:embedding AS vector)) AS score
                FROM source_spans AS span
                JOIN source_revisions AS revision ON revision.id = span.source_revision_id
                JOIN sources AS source ON source.id = revision.source_id
                WHERE source.wiki_id = :wiki_id
                  AND span.embedding IS NOT NULL
                  AND span.embedding_profile_id = :embedding_profile_id
                ORDER BY span.embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
                """
            ),
            {
                "embedding": json.dumps(list(embedding)),
                "embedding_profile_id": embedding_profile_id,
                "wiki_id": self.wiki_id,
                "limit": limit,
            },
        ).mappings()
        return [
            RankedHit(
                document_id=f"raw:{row['id']}",
                channel="raw_vector",
                rank=index,
                source_score=float(row["score"]),
                content=row["content"],
                metadata={
                    "layer": "raw",
                    "span_id": row["id"],
                    "source_revision_id": row["source_revision_id"],
                    "heading": row["heading"],
                    "locator": _as_dict(row["locator"]),
                },
            )
            for index, row in enumerate(rows, start=1)
        ]


def hybrid_search(
    repository: WikiSearchRepository,
    query: str,
    *,
    embedding: Sequence[float] | None = None,
    entry_node_ids: Sequence[str] = (),
    limit: int = 10,
) -> list[FusedHit]:
    bm25_hits = repository.bm25(query)
    inferred_entry_ids = [
        page_id
        for hit in bm25_hits[:5]
        if isinstance((page_id := hit.metadata.get("id")), str)
    ]
    graph_entry_ids = tuple(dict.fromkeys([*entry_node_ids, *inferred_entry_ids]))
    channels = [
        bm25_hits,
        repository.graph(graph_entry_ids),
    ]
    if embedding is not None:
        channels.append(repository.vector(embedding))
    return reciprocal_rank_fusion(channels, limit=limit)


def raw_hybrid_search(
    repository: RawSearchRepository,
    query: str,
    *,
    embedding: Sequence[float] | None = None,
    embedding_profile_id: str | None = None,
    limit: int = 10,
) -> list[FusedHit]:
    channels = [repository.bm25(query)]
    if embedding is not None and embedding_profile_id is not None:
        channels.append(repository.vector(embedding, embedding_profile_id))
    return reciprocal_rank_fusion(channels, limit=limit)


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    raise ValueError("search metadata is not an object")
