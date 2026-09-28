from fastapi import APIRouter
from sqlalchemy import desc, func, select

from wiki_agent.api.dependencies import AdminUser, DbSession
from wiki_agent.models import QueryLog

router = APIRouter(prefix="/knowledge-gaps", tags=["knowledge-gaps"])


@router.get("")
def list_knowledge_gaps(
    user: AdminUser,
    db: DbSession,
    limit: int = 50,
) -> list[dict[str, object]]:
    bounded_limit = min(max(limit, 1), 200)
    rows = db.execute(
        select(
            QueryLog.query,
            func.count(QueryLog.id).label("occurrences"),
            func.max(QueryLog.created_at).label("last_seen"),
            func.max(QueryLog.hit_count).label("best_hit_count"),
        )
        .where((QueryLog.fallback_used.is_(True)) | (QueryLog.hit_count == 0))
        .group_by(QueryLog.query)
        .order_by(desc("occurrences"), desc("last_seen"))
        .limit(bounded_limit)
    ).mappings()
    return [
        {
            "query": row["query"],
            "occurrences": row["occurrences"],
            "last_seen": row["last_seen"],
            "best_hit_count": row["best_hit_count"],
        }
        for row in rows
    ]
