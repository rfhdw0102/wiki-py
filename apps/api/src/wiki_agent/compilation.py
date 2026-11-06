from __future__ import annotations

from celery import chain
from sqlalchemy import select
from sqlalchemy.orm import Session

from wiki_agent.config import Settings
from wiki_agent.llm import ModelFactory
from wiki_agent.models import (
    AgentEvent,
    AgentRun,
    FeatureFlag,
    ModelKind,
    ModelProfile,
    Source,
    SourceRevision,
)
from wiki_agent.tasks import compile_source, prepare_compilation_source_task


class CompilationConfigurationError(ValueError):
    pass


class CompilationQueueError(RuntimeError):
    pass


def enqueue_compilation(
    db: Session,
    settings: Settings,
    *,
    source_revision_id: str,
    created_by_id: str,
    wiki_id: str,
    publication_policy: str,
    model_profile_id: str | None = None,
    raise_on_dispatch_error: bool = True,
    source_revision_ids: list[str] | None = None,
) -> AgentRun:
    revision_ids = list(dict.fromkeys(
        [source_revision_id] if source_revision_ids is None else source_revision_ids
    ))
    if not revision_ids or revision_ids[0] != source_revision_id:
        raise CompilationConfigurationError("Invalid compilation batch")
    for revision_id in revision_ids:
        revision = db.get(SourceRevision, revision_id)
        source = db.get(Source, revision.source_id) if revision is not None else None
        if revision is None or source is None or source.wiki_id != wiki_id:
            raise CompilationConfigurationError("Source revision not found")
    if source_revision_ids is not None:
        publication_policy = "manual_review"
    profile_id = _resolve_profile_id(
        db,
        settings,
        created_by_id,
        model_profile_id,
    )
    if publication_policy == "low_risk_auto_publish":
        auto_publish = db.get(FeatureFlag, "auto_publish")
        if auto_publish is None or not auto_publish.enabled:
            raise CompilationConfigurationError("Automatic publication is disabled")

    run = AgentRun(
        wiki_id=wiki_id,
        created_by_id=created_by_id,
        kind="compile_source",
        input_json={
            "source_revision_id": source_revision_id,
            "model_profile_id": profile_id,
            "publication_policy": publication_policy,
            **({"source_revision_ids": revision_ids, "batch": True}
               if source_revision_ids is not None else {}),
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
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="run.failed",
                payload={"error": run.error},
            )
        )
        db.commit()
        if raise_on_dispatch_error:
            raise CompilationQueueError(run.error) from exc
    return run


def enqueue_ingested_batch(
    db: Session,
    settings: Settings,
    *,
    source_revision_ids: list[str],
    created_by_id: str,
    wiki_id: str,
) -> AgentRun:
    if not source_revision_ids:
        raise CompilationConfigurationError("Compilation batch must not be empty")
    try:
        return enqueue_compilation(
            db, settings,
            source_revision_id=source_revision_ids[0],
            source_revision_ids=source_revision_ids,
            created_by_id=created_by_id,
            wiki_id=wiki_id,
            publication_policy="manual_review",
            raise_on_dispatch_error=False,
        )
    except CompilationConfigurationError as exc:
        run = AgentRun(
            wiki_id=wiki_id, created_by_id=created_by_id, kind="compile_source",
            status="failed",
            input_json={
                "source_revision_id": source_revision_ids[0],
                "source_revision_ids": source_revision_ids,
                "batch": True,
                "model_profile_id": None,
                "publication_policy": "manual_review",
            },
            error=str(exc),
        )
        db.add(run)
        db.flush()
        db.add(AgentEvent(run_id=run.id, kind="run.failed", payload={"error": run.error}))
        db.commit()
        db.refresh(run)
        return run


def enqueue_ingested_revision(
    db: Session,
    settings: Settings,
    *,
    source_revision_id: str,
    created_by_id: str,
    wiki_id: str,
) -> AgentRun:
    auto_publish = db.get(FeatureFlag, "auto_publish")
    publication_policy = (
        "low_risk_auto_publish"
        if auto_publish is not None and auto_publish.enabled
        else "manual_review"
    )
    try:
        return enqueue_compilation(
            db,
            settings,
            source_revision_id=source_revision_id,
            created_by_id=created_by_id,
            wiki_id=wiki_id,
            publication_policy=publication_policy,
            raise_on_dispatch_error=False,
        )
    except CompilationConfigurationError as exc:
        run = AgentRun(
            wiki_id=wiki_id,
            created_by_id=created_by_id,
            kind="compile_source",
            status="failed",
            input_json={
                "source_revision_id": source_revision_id,
                "model_profile_id": None,
                "publication_policy": publication_policy,
            },
            error=str(exc),
        )
        db.add(run)
        db.flush()
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="run.failed",
                payload={"error": run.error},
            )
        )
        db.commit()
        db.refresh(run)
        return run


def _resolve_profile_id(
    db: Session,
    settings: Settings,
    user_id: str,
    requested_profile_id: str | None,
) -> str:
    factory = ModelFactory(db, settings, user_id)
    profile_id = requested_profile_id or factory.assigned_profile_id("compilation")
    if profile_id is None:
        raise CompilationConfigurationError(
            "No compilation model is assigned and model_profile_id was not provided"
        )
    profile = db.scalar(
        select(ModelProfile).where(
            ModelProfile.id == profile_id,
            ModelProfile.owner_id == user_id,
        )
    )
    if profile is None:
        raise CompilationConfigurationError("Model profile not found")
    if profile.kind is not ModelKind.CHAT or not profile.enabled:
        raise CompilationConfigurationError(
            "Compilation requires an enabled chat model"
        )
    return profile_id
