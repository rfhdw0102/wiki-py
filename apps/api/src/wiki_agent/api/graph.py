from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text

from wiki_agent.api.dependencies import CurrentUser, DbSession, require_feature
from wiki_agent.models import KnowledgeNode

router = APIRouter(
    prefix="/graph",
    tags=["graph"],
    dependencies=[Depends(require_feature("graph_view"))],
)


@router.get("/nodes")
def list_nodes(
    user: CurrentUser,
    db: DbSession,
    kind: str | None = None,
    query: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, str]]:
    statement = select(KnowledgeNode)
    if kind:
        statement = statement.where(KnowledgeNode.kind == kind)
    if query:
        statement = statement.where(KnowledgeNode.title.ilike(f"%{query}%"))
    nodes = db.scalars(statement.order_by(KnowledgeNode.title).limit(limit))
    return [
        {"id": node.id, "path": node.path, "kind": node.kind, "title": node.title}
        for node in nodes
    ]


@router.get("/traverse/{node_id}")
def traverse_graph(
    node_id: str,
    user: CurrentUser,
    db: DbSession,
    max_depth: Annotated[int, Query(ge=1, le=5)] = 2,
) -> dict[str, object]:
    rows = db.execute(
        text(
            """
            WITH RECURSIVE traversal(node_id, depth, path) AS (
                SELECT id, 0, ',' || id || ','
                FROM knowledge_nodes
                WHERE id = :node_id
                UNION ALL
                SELECT edge.target_node_id,
                       traversal.depth + 1,
                       traversal.path || edge.target_node_id || ','
                FROM traversal
                JOIN knowledge_edges edge ON edge.source_node_id = traversal.node_id
                WHERE traversal.depth < :max_depth
                  AND traversal.path NOT LIKE '%,' || edge.target_node_id || ',%'
            )
            SELECT node.id, node.path, node.kind, node.title, MIN(traversal.depth) AS depth
            FROM traversal
            JOIN knowledge_nodes node ON node.id = traversal.node_id
            GROUP BY node.id, node.path, node.kind, node.title
            ORDER BY depth, node.title
            """
        ),
        {"node_id": node_id, "max_depth": max_depth},
    ).mappings()
    nodes = [
        {
            "id": row["id"],
            "path": row["path"],
            "kind": row["kind"],
            "title": row["title"],
            "depth": row["depth"],
        }
        for row in rows
    ]
    return {"root_id": node_id, "max_depth": max_depth, "nodes": nodes}
