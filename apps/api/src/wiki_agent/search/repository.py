import json
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from wiki_agent.search.fusion import FusedHit, RankedHit, reciprocal_rank_fusion


class WikiSearchRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def bm25(self, query: str, limit: int = 20) -> list[RankedHit]:
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            statement = text(
                """
                SELECT path, content, metadata_json, paradedb.score(path) AS score
                FROM wiki_documents
                WHERE title @@@ :query OR content @@@ :query
                ORDER BY score DESC
                LIMIT :limit
                """
            )
        else:
            statement = text(
                """
                SELECT path, content, metadata_json, 1.0 AS score
                FROM wiki_documents
                WHERE lower(title) LIKE lower(:pattern)
                   OR lower(content) LIKE lower(:pattern)
                ORDER BY title
                LIMIT :limit
                """
            )
        parameters: dict[str, object] = {"query": query, "limit": limit}
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
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
                """
            ),
            {"embedding": json.dumps(list(embedding)), "limit": limit},
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
        parameters["max_depth"] = max_depth
        rows = self.db.execute(
            text(
                f"""
                WITH RECURSIVE traversal(node_id, depth) AS (
                    SELECT id, 0 FROM knowledge_nodes WHERE id IN ({placeholders})
                    UNION
                    SELECT edge.target_node_id, traversal.depth + 1
                    FROM traversal
                    JOIN knowledge_edges edge ON edge.source_node_id = traversal.node_id
                    WHERE traversal.depth < :max_depth
                )
                SELECT document.path, document.content, document.metadata_json, reached.depth
                FROM (
                    SELECT node_id, MIN(depth) AS depth
                    FROM traversal
                    GROUP BY node_id
                ) reached
                JOIN wiki_documents document ON document.page_id = reached.node_id
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
    def __init__(self, db: Session) -> None:
        self.db = db

    def bm25(self, query: str, limit: int = 20) -> list[RankedHit]:
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            statement = text(
                """
                SELECT id, source_revision_id, heading, content, locator,
                       paradedb.score(id) AS score
                FROM source_spans
                WHERE heading @@@ :query OR content @@@ :query
                ORDER BY score DESC
                LIMIT :limit
                """
            )
            parameters: dict[str, object] = {"query": query, "limit": limit}
        else:
            statement = text(
                """
                SELECT id, source_revision_id, heading, content, locator, 1.0 AS score
                FROM source_spans
                WHERE lower(COALESCE(heading, '')) LIKE lower(:pattern)
                   OR lower(content) LIKE lower(:pattern)
                ORDER BY id
                LIMIT :limit
                """
            )
            parameters = {"pattern": f"%{query}%", "limit": limit}
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

    def vector(self, embedding: Sequence[float], limit: int = 20) -> list[RankedHit]:
        if not embedding or self.db.bind is None or self.db.bind.dialect.name != "postgresql":
            return []
        rows = self.db.execute(
            text(
                """
                SELECT id, source_revision_id, heading, content, locator,
                       1 - (embedding <=> CAST(:embedding AS vector)) AS score
                FROM source_spans
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
                """
            ),
            {"embedding": json.dumps(list(embedding)), "limit": limit},
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
    limit: int = 10,
) -> list[FusedHit]:
    channels = [repository.bm25(query)]
    if embedding is not None:
        channels.append(repository.vector(embedding))
    return reciprocal_rank_fusion(channels, limit=limit)


def _as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    raise ValueError("search metadata is not an object")
