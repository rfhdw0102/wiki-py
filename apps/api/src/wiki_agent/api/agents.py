import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from wiki_agent.api.dependencies import CurrentUser, CurrentWiki, DbSession, require_feature
from wiki_agent.api.schemas import AgentRunView, CompileSourceRequest, ParseSourceRequest
from wiki_agent.compilation import (
    CompilationConfigurationError,
    CompilationQueueError,
    enqueue_compilation,
)
from wiki_agent.config import get_settings
from wiki_agent.db import SessionLocal
from wiki_agent.llm import ModelFactory
from wiki_agent.llm.factory import ModelConfigurationError
from wiki_agent.models import (
    AgentEvent,
    AgentRun,
    Source,
    SourceRevision,
)
from wiki_agent.sources.crawler import CrawlRequest
from wiki_agent.tasks import (
    backfill_source_embeddings_task,
    crawl_site_task,
    parse_source_revision_task,
)

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/runs/compile", response_model=AgentRunView, status_code=status.HTTP_202_ACCEPTED)
def queue_compilation(
    payload: CompileSourceRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> AgentRun:
    revision = db.get(SourceRevision, payload.source_revision_id)
    source = db.get(Source, revision.source_id) if revision is not None else None
    if revision is None or source is None or source.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Source revision not found")
    try:
        return enqueue_compilation(
            db,
            get_settings(),
            source_revision_id=payload.source_revision_id,
            created_by_id=user.id,
            wiki_id=wiki.id,
            publication_policy=payload.publication_policy,
            model_profile_id=payload.model_profile_id,
        )
    except CompilationConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CompilationQueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/runs/crawl",
    response_model=AgentRunView,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_feature("website_crawl"))],
)
def queue_site_crawl(
    payload: CrawlRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> AgentRun:
    run = AgentRun(
        wiki_id=wiki.id,
        created_by_id=user.id,
        kind="crawl_site",
        input_json=payload.model_dump(mode="json"),
    )
    db.add(run)
    db.flush()
    db.add(AgentEvent(run_id=run.id, kind="run.queued", payload={}))
    db.commit()
    db.refresh(run)
    try:
        crawl_site_task.apply_async(args=[run.id], queue="default")
    except Exception as exc:
        run.status = "failed"
        run.error = f"Queue submission failed: {exc}"
        db.commit()
        raise HTTPException(status_code=503, detail=run.error) from exc
    return run


@router.post("/runs/parse", response_model=AgentRunView, status_code=status.HTTP_202_ACCEPTED)
def queue_source_parse(
    payload: ParseSourceRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> AgentRun:
    revision = db.get(SourceRevision, payload.source_revision_id)
    source = db.get(Source, revision.source_id) if revision is not None else None
    if revision is None or source is None or source.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Source revision not found")
    run = AgentRun(
        wiki_id=wiki.id,
        created_by_id=user.id,
        kind="parse_source",
        input_json=payload.model_dump(mode="json"),
    )
    db.add(run)
    db.flush()
    db.add(AgentEvent(run_id=run.id, kind="run.queued", payload={}))
    db.commit()
    db.refresh(run)
    try:
        parse_source_revision_task.apply_async(args=[run.id], queue="parser")
    except Exception as exc:
        run.status = "failed"
        run.error = f"Queue submission failed: {exc}"
        db.commit()
        raise HTTPException(status_code=503, detail=run.error) from exc
    return run


@router.post(
    "/runs/backfill-embeddings",
    response_model=AgentRunView,
    status_code=status.HTTP_202_ACCEPTED,
)
def queue_source_embedding_backfill(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> AgentRun:
    factory = ModelFactory(db, get_settings(), user.id)
    profile_id = factory.assigned_profile_id("embedding")
    if profile_id is None:
        raise HTTPException(
            status_code=422,
            detail="No embedding model is assigned.",
        )
    try:
        factory.embeddings(profile_id)
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = AgentRun(
        wiki_id=wiki.id,
        created_by_id=user.id,
        kind="backfill_source_embeddings",
        input_json={
            "embedding_profile_id": profile_id,
            "batch_size": 64,
        },
    )
    db.add(run)
    db.flush()
    db.add(AgentEvent(run_id=run.id, kind="run.queued", payload={}))
    db.commit()
    db.refresh(run)
    try:
        backfill_source_embeddings_task.apply_async(args=[run.id], queue="default")
    except Exception as exc:
        run.status = "failed"
        run.error = f"Queue submission failed: {exc}"
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="run.failed",
                payload={"error": run.error},
            )
        )
        db.commit()
        raise HTTPException(status_code=503, detail=run.error) from exc
    return run


@router.get("/runs", response_model=list[AgentRunView])
def list_runs(user: CurrentUser, wiki: CurrentWiki, db: DbSession) -> list[AgentRun]:
    return list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.created_by_id == user.id, AgentRun.wiki_id == wiki.id)
            .order_by(AgentRun.created_at.desc())
        )
    )


@router.get("/runs/{run_id}", response_model=AgentRunView)
def get_run(
    run_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> AgentRun:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Agent run belongs to another user")
    if run.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return run


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Agent run belongs to another user")
    if run.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Agent run not found")
    try:
        cursor = int(last_event_id or 0)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        nonlocal cursor
        while not await request.is_disconnected():
            with SessionLocal() as event_db:
                events = list(
                    event_db.scalars(
                        select(AgentEvent)
                        .where(AgentEvent.run_id == run_id, AgentEvent.id > cursor)
                        .order_by(AgentEvent.id)
                    )
                )
                current_run = event_db.get(AgentRun, run_id)
            for event in events:
                cursor = event.id
                yield {
                    "id": str(event.id),
                    "event": event.kind,
                    "data": json.dumps(event.payload, ensure_ascii=False),
                }
            if current_run is None or (
                current_run.status in {"failed", "completed", "awaiting_review"} and not events
            ):
                break
            await asyncio.sleep(1)

    return EventSourceResponse(event_stream())
