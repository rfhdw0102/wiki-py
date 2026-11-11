from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import String, case, cast, func, literal, or_, select, union_all

from wiki_agent.api.dependencies import CurrentUser, CurrentWiki, DbSession
from wiki_agent.api.log_context import enrich_log_context
from wiki_agent.models import AgentRun, ApiErrorLog, AuditLog, QuestionRun

router = APIRouter(prefix="/logs", tags=["logs"])
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")

AUDIT_SOURCE_BY_RESOURCE = {
    "agent_run": "后台任务",
    "auth_session": "账号认证",
    "conversation": "知识问答",
    "draft": "Wiki 审核",
    "knowledge_schema": "知识规范",
    "model_assignment": "模型配置",
    "model_profile": "模型配置",
    "source": "原件管理",
    "source_revision": "原件管理",
    "user": "账号资料",
    "wiki": "知识库",
    "wiki_commit": "知识库",
}


@router.get("")
def list_logs(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    category: Literal["all", "operation", "error"] = "all",
    source: str = "all",
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")

    operations = select(
        AuditLog.id.label("id"),
        case(
            (AuditLog.action.in_(["wiki.index_failed", "source.web_failed"]), "error"),
            else_="operation",
        ).label("category"),
        case(AUDIT_SOURCE_BY_RESOURCE, value=AuditLog.resource_type, else_="个人操作").label(
            "source"
        ),
        AuditLog.action.label("event"),
        AuditLog.resource_type.label("resource_type"),
        AuditLog.resource_id.label("resource_id"),
        literal(None, type_=String).label("method"),
        literal(None, type_=String).label("path"),
        cast(AuditLog.details, String).label("details"),
        AuditLog.created_at.label("created_at"),
    ).where(AuditLog.actor_id == user.id)

    api_errors = select(
        ApiErrorLog.id.label("id"),
        literal("error").label("category"),
        literal("API 接口").label("source"),
        ApiErrorLog.error_type.label("event"),
        literal("api_request", type_=String).label("resource_type"),
        literal(None, type_=String).label("resource_id"),
        ApiErrorLog.method.label("method"),
        ApiErrorLog.path.label("path"),
        ApiErrorLog.message.label("details"),
        ApiErrorLog.created_at.label("created_at"),
    ).where(ApiErrorLog.user_id == user.id)

    agent_errors = select(
        AgentRun.id.label("id"),
        literal("error").label("category"),
        literal("后台任务").label("source"),
        AgentRun.kind.label("event"),
        literal("agent_run", type_=String).label("resource_type"),
        AgentRun.id.label("resource_id"),
        literal(None, type_=String).label("method"),
        literal(None, type_=String).label("path"),
        AgentRun.error.label("details"),
        AgentRun.updated_at.label("created_at"),
    ).where(
        AgentRun.wiki_id == wiki.id,
        AgentRun.created_by_id == user.id,
        AgentRun.status.in_(("failed", "published_index_failed")),
        AgentRun.error.is_not(None),
    )

    question_runs = select(
        QuestionRun.id.label("id"),
        case(
            (QuestionRun.status == "failed", "error"),
            else_="operation",
        ).label("category"),
        literal("知识问答").label("source"),
        literal("question", type_=String).label("event"),
        literal("question_run", type_=String).label("resource_type"),
        literal(None, type_=String).label("resource_id"),
        literal(None, type_=String).label("method"),
        literal(None, type_=String).label("path"),
        case(
            (QuestionRun.status == "failed", QuestionRun.error),
            else_=QuestionRun.question,
        ).label("details"),
        QuestionRun.updated_at.label("created_at"),
    ).where(
        QuestionRun.wiki_id == wiki.id,
        QuestionRun.user_id == user.id,
        QuestionRun.status.in_(("completed", "failed")),
    )

    records = union_all(operations, api_errors, agent_errors, question_runs).subquery()
    query = select(records)
    conditions = []
    if category != "all":
        conditions.append(records.c.category == category)
    if source != "all":
        conditions.append(records.c.source == source)
    if date_from:
        conditions.append(
            records.c.created_at
            >= datetime.combine(date_from, time.min, LOCAL_TIMEZONE).astimezone(UTC)
        )
    if date_to:
        end_exclusive = datetime.combine(
            date_to + timedelta(days=1), time.min, LOCAL_TIMEZONE
        ).astimezone(UTC)
        conditions.append(records.c.created_at < end_exclusive)
    if search and (term := search.strip()):
        pattern = f"%{term}%"
        conditions.append(or_(
            records.c.event.ilike(pattern),
            records.c.source.ilike(pattern),
            records.c.resource_type.ilike(pattern),
            records.c.resource_id.ilike(pattern),
            records.c.method.ilike(pattern),
            records.c.path.ilike(pattern),
            records.c.details.ilike(pattern),
        ))

    query = query.where(*conditions)
    count_query = select(func.count()).select_from(records).where(*conditions)
    total = db.scalar(count_query) or 0
    rows = db.execute(
        query.order_by(records.c.created_at.desc(), records.c.id.desc())
        .offset(offset)
        .limit(limit)
    )
    items: list[dict[str, object]] = [
        {
            "id": row.id,
            "actor_name": user.display_name,
            "category": row.category,
            "source": row.source,
            "event": row.event,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "method": row.method,
            "path": row.path,
            "details": row.details,
            "created_at": row.created_at,
        }
        for row in rows
    ]
    enrich_log_context(items, db, wiki.id, user.id)
    return {"items": items, "total": total, "offset": offset, "limit": limit}
