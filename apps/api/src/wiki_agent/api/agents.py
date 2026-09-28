import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from celery import chain
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from wiki_agent.api.dependencies import CurrentUser, DbSession, require_feature
from wiki_agent.api.schemas import AgentRunView, CompileSourceRequest, ParseSourceRequest
from wiki_agent.config import get_settings
from wiki_agent.db import SessionLocal
from wiki_agent.llm import ModelFactory
from wiki_agent.models import (
    AgentEvent,
    AgentRun,
    FeatureFlag,
    ModelKind,
    ModelProfile,
    SourceRevision,
)
from wiki_agent.sources.crawler import CrawlRequest
from wiki_agent.tasks import (
    compile_source,
    crawl_site_task,
    parse_source_revision_task,
    prepare_compilation_source_task,
)

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/runs/compile", response_model=AgentRunView, status_code=status.HTTP_202_ACCEPTED)
def queue_compilation(
    payload: CompileSourceRequest,
    user: CurrentUser,
    db: DbSession,
) -> AgentRun:
    if db.get(SourceRevision, payload.source_revision_id) is None:
        raise HTTPException(status_code=404, detail="Source revision not found")
    factory = ModelFactory(db, get_settings())
    profile_id = payload.model_profile_id or factory.assigned_profile_id("compilation")
    if profile_id is None:
        raise HTTPException(
            status_code=422,
            detail="No compilation model is assigned and model_profile_id was not provided",
        )
    profile = db.get(ModelProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Model profile not found")
    if profile.kind is not ModelKind.CHAT or not profile.enabled:
        raise HTTPException(status_code=422, detail="Compilation requires an enabled chat model")
    if payload.publication_policy == "low_risk_auto_publish":
        auto_publish = db.get(FeatureFlag, "auto_publish")
        if auto_publish is None or not auto_publish.enabled:
            raise HTTPException(status_code=403, detail="Automatic publication is disabled")
    run = AgentRun(
        created_by_id=user.id,
        kind="compile_source",
        input_json={
            **payload.model_dump(mode="json"),
            "model_profile_id": profile_id,
        },
    )
    db.add(run)
    db.flush()
    db.add(AgentEvent(run_id=run.id, kind="run.queued", payload={}))
    db.commit()
    db.refresh(run)
    try:
        chain(
            prepare_compilation_source_task.si(run.id).set(queue="parser"),
            compile_source.si(run.id).set(queue="default"),
        ).apply_async()
    except Exception as exc:
        run.status = "failed"
        run.error = f"Queue submission failed: {exc}"
        db.commit()
        raise HTTPException(status_code=503, detail=run.error) from exc
    return run


@router.post(
    "/runs/crawl",
    response_model=AgentRunView,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_feature("website_crawl"))],
)
def queue_site_crawl(
    payload: CrawlRequest,
    user: CurrentUser,
    db: DbSession,
) -> AgentRun:
    run = AgentRun(
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
    db: DbSession,
) -> AgentRun:
    if db.get(SourceRevision, payload.source_revision_id) is None:
        raise HTTPException(status_code=404, detail="Source revision not found")
    run = AgentRun(
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


@router.get("/runs", response_model=list[AgentRunView])
def list_runs(user: CurrentUser, db: DbSession) -> list[AgentRun]:
    return list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.created_by_id == user.id)
            .order_by(AgentRun.created_at.desc())
        )
    )


@router.get("/runs/{run_id}", response_model=AgentRunView)
def get_run(run_id: str, user: CurrentUser, db: DbSession) -> AgentRun:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Agent run belongs to another user")
    return run


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    user: CurrentUser,
    db: DbSession,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> EventSourceResponse:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Agent run not found")
    if run.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Agent run belongs to another user")
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
