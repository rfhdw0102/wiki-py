import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import String, column, delete, inspect, select, table
from sqlalchemy.exc import SQLAlchemyError

from wiki_agent.api.dependencies import (
    AppSettings,
    CurrentUser,
    CurrentWiki,
    DbSession,
    require_feature,
)
from wiki_agent.api.schemas import (
    AgentRunView,
    CompileBatchRequest,
    SourceBatchIngestView,
    SourceBatchItemView,
    SourceIngestView,
    SourceRevisionView,
    SourceSpanView,
    SourceView,
    WebsiteSourceCreate,
)
from wiki_agent.compilation import (
    CompilationConfigurationError,
    CompilationQueueError,
    enqueue_compilation,
    enqueue_ingested_batch,
    enqueue_ingested_revision,
)
from wiki_agent.models import (
    AgentEvent,
    AgentRun,
    AuditLog,
    Draft,
    FeatureFlag,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
)
from wiki_agent.sources import SourceObjectStore
from wiki_agent.sources.article import WebContentError, extract_web_article
from wiki_agent.sources.browser import BrowserClient
from wiki_agent.sources.fetcher import UnsafeSourceUrlError, fetch_page
from wiki_agent.sources.storage import SourceTooLargeError
from wiki_agent.sources.web_import import store_web_source
from wiki_agent.wiki import WikiPage, WikiRepository, open_wiki_repository
from wiki_agent.wiki.deletion import remove_source_references
from wiki_agent.wiki.indexer import WikiIndexer
from wiki_agent.wiki.repository import StaleWikiRevisionError, WikiRepositoryError

router = APIRouter(prefix="/sources", tags=["sources"])
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS: dict[str, tuple[SourceKind, set[str]]] = {
    ".pdf": (SourceKind.PDF, {"application/pdf"}),
    ".docx": (
        SourceKind.DOCX,
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ),
    ".md": (SourceKind.MARKDOWN, {"text/markdown", "text/plain", "application/octet-stream"}),
    ".txt": (SourceKind.TEXT, {"text/plain", "application/octet-stream"}),
    ".html": (SourceKind.HTML, {"text/html", "application/xhtml+xml"}),
    ".htm": (SourceKind.HTML, {"text/html", "application/xhtml+xml"}),
}


def _validate_upload(upload: UploadFile) -> SourceKind:
    suffix = Path(upload.filename or "").suffix.lower()
    allowed = ALLOWED_EXTENSIONS.get(suffix)
    if allowed is None:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension: {suffix or 'none'}",
        )
    kind, media_types = allowed
    if upload.content_type not in media_types:
        raise HTTPException(
            status_code=415,
            detail=f"Content type {upload.content_type!r} does not match {suffix}",
        )
    return kind


def _ingest_view(
    revision: SourceRevision,
    run_id: str,
    run_status: str,
    error: str | None,
) -> SourceIngestView:
    return SourceIngestView(
        id=revision.id,
        source_id=revision.source_id,
        sha256=revision.sha256,
        media_type=revision.media_type,
        size_bytes=revision.size_bytes,
        status=revision.status,
        created_at=revision.created_at,
        compilation_run_id=run_id,
        compilation_status=run_status,
        compilation_error=error,
    )


@router.get("", response_model=list[SourceView])
def list_sources(user: CurrentUser, wiki: CurrentWiki, db: DbSession) -> list[Source]:
    return list(
        db.scalars(
            select(Source)
            .where(Source.wiki_id == wiki.id)
            .order_by(Source.created_at.desc())
        )
    )


def _owned_source(db: DbSession, source_id: str, wiki_id: str) -> Source:
    source = db.scalar(
        select(Source)
        .where(Source.id == source_id, Source.wiki_id == wiki_id)
        .with_for_update()
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


class SourceRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=512, strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def validate_name(cls, value: object) -> object:
        if isinstance(value, str):
            if any(char in value for char in "/\\\r\n\v\f\x1c\x1d\x1e\u0085\u2028\u2029"):
                raise ValueError("Source name must not contain line breaks or path separators")
            return value.strip()
        return value


@router.patch("/{source_id}", response_model=SourceView)
def rename_source(
    source_id: str,
    payload: SourceRenameRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> Source:
    try:
        source = _owned_source(db, source_id, wiki.id)
        previous_name = source.name
        source.name = payload.name
        db.add(AuditLog(
            actor_id=user.id,
            action="source.renamed",
            resource_type="source",
            resource_id=source.id,
            details={"old_name": previous_name, "name": source.name, "wiki_id": wiki.id},
        ))
        db.commit()
        db.refresh(source)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            "Source rename failed", extra={"source_id": source_id, "wiki_id": wiki.id}
        )
        raise HTTPException(
            status_code=500, detail="Source rename failed; refresh and retry"
        ) from exc
    return source


def _revision_runs(db: DbSession, wiki_id: str, revision_ids: list[str]) -> list[AgentRun]:
    if not revision_ids:
        return []
    revisions = set(revision_ids)
    return [
        run for run in db.scalars(
            select(AgentRun).where(
                AgentRun.wiki_id == wiki_id,
                AgentRun.kind.in_(("compile_source", "parse_source")),
            ).order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        )
        if revisions.intersection(run.source_revision_ids)
    ]


def _references_revision(page: WikiPage, path: str, revision_ids: set[str]) -> bool:
    return (
        path in {f"sources/{revision_id}.md" for revision_id in revision_ids}
        or bool(
            set(page.metadata.sources) & {
                f"sources/{revision_id}.md" for revision_id in revision_ids
            }
        )
        or bool(
            set(page.links) & {f"sources/{revision_id}" for revision_id in revision_ids}
        )
        or any(
            citation.source_revision_id in revision_ids
            for citation in page.metadata.citations.values()
        )
    )


@router.post(
    "/{source_id}/retry",
    response_model=AgentRunView,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_source(
    source_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
) -> AgentRun:
    db.scalar(select(PersonalWiki.id).where(PersonalWiki.id == wiki.id).with_for_update())
    source = _owned_source(db, source_id, wiki.id)
    revision_id = _retryable_revision(source, wiki.id, db, settings)
    latest = next(
        (run for run in _revision_runs(db, wiki.id, [revision_id]) if run.kind == "compile_source"),
        None,
    )
    batch_ids = latest.source_revision_ids if latest and latest.input_json.get("batch") else None
    if batch_ids is not None:
        for member in batch_ids:
            revision = db.get(SourceRevision, member)
            if revision is None:
                raise HTTPException(status_code=409, detail="Compilation batch source is missing")
            member_source = _owned_source(db, revision.source_id, wiki.id)
            _retryable_revision(member_source, wiki.id, db, settings)
    flag = db.get(FeatureFlag, "auto_publish")
    policy = "low_risk_auto_publish" if flag is not None and flag.enabled else "manual_review"
    try:
        return enqueue_compilation(
            db, settings, source_revision_id=batch_ids[0] if batch_ids else revision_id,
            source_revision_ids=batch_ids,
            created_by_id=user.id, wiki_id=wiki.id, publication_policy=policy,
        )
    except CompilationConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CompilationQueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _retryable_revision(
    source: Source, wiki_id: str, db: DbSession, settings: AppSettings
) -> str:
    if not source.current_revision_id:
        raise HTTPException(status_code=409, detail="Source has no revision to compile")
    runs = _revision_runs(db, wiki_id, [source.current_revision_id])
    latest = next((run for run in runs if run.kind == "compile_source"), None)
    if any(
        run.status in {"completed", "published_index_failed"}
        or (run.result_json or {}).get("published_commit")
        for run in runs
    ):
        raise HTTPException(status_code=409, detail="Source has already been published")
    repository = open_wiki_repository(
        settings.wiki_library_root(wiki_id),
        settings.legacy_wiki_library_root(wiki_id),
    )
    if f"sources/{source.current_revision_id}.md" in repository.list_pages():
        raise HTTPException(status_code=409, detail="Source has already been published")
    for run in runs:
        if run.status != "awaiting_review":
            continue
        draft_id = (run.result_json or {}).get("draft_id")
        draft = db.get(Draft, draft_id) if isinstance(draft_id, str) else None
        if draft is None or draft.status.value != "rejected":
            raise HTTPException(status_code=409, detail="Compilation is awaiting review")
    if latest is not None and latest.status not in {"failed", "awaiting_review"}:
        raise HTTPException(
            status_code=409, detail="Compilation is already in progress or published"
        )
    if any(
        run.status in {"queued", "parsing", "running", "publishing"}
        for run in runs
    ):
        raise HTTPException(status_code=409, detail="Source is still being processed")
    return source.current_revision_id


@router.post("/compile-batch", response_model=AgentRunView, status_code=202)
def compile_source_batch(
    payload: CompileBatchRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
) -> AgentRun:
    db.scalar(select(PersonalWiki.id).where(PersonalWiki.id == wiki.id).with_for_update())
    ids = sorted(set(payload.source_ids))
    sources = [_owned_source(db, source_id, wiki.id) for source_id in ids]
    revisions = [_retryable_revision(source, wiki.id, db, settings) for source in sources]
    existing_batches = {
        member
        for revision in revisions
        for run in _revision_runs(db, wiki.id, [revision])[:1]
        if run.input_json.get("batch")
        for member in run.source_revision_ids
    }
    for revision_id in sorted(existing_batches - set(revisions)):
        revision = db.get(SourceRevision, revision_id)
        if revision is None:
            raise HTTPException(status_code=409, detail="Compilation batch source is missing")
        source = _owned_source(db, revision.source_id, wiki.id)
        revisions.append(_retryable_revision(source, wiki.id, db, settings))
    try:
        return enqueue_compilation(
            db,
            settings,
            source_revision_id=revisions[0],
            source_revision_ids=revisions,
            created_by_id=user.id,
            wiki_id=wiki.id,
            publication_policy="manual_review",
        )
    except CompilationConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CompilationQueueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
) -> None:
    deletion = prepare_source_deletion(
        source_id, wiki.id, db, settings, remove_references=True,
    )
    wiki_cleaned = False
    try:
        for draft_id, files in deletion.draft_updates.items():
            draft = db.get(Draft, draft_id)
            if draft is not None:
                if files:
                    draft.files = files
                else:
                    db.delete(draft)
        delete_source_records(deletion, user.id, db)
        db.flush()
        repository = deletion.repository
        if repository is not None:
            if deletion.wiki_changes:
                result = repository.publish(
                    deletion.wiki_changes,
                    base_commit=deletion.base_commit,
                    author_name=user.display_name,
                    author_email=user.local_commit_email,
                    message=f"Delete source: {deletion.source.name}",
                )
                wiki_cleaned = True
                for draft_id in deletion.draft_updates:
                    draft = db.get(Draft, draft_id)
                    if draft is not None and draft.base_commit == deletion.base_commit:
                        draft.base_commit = result.commit
            WikiIndexer(repository, wiki.id).rebuild(
                db, reuse_embeddings=True, commit_transaction=False,
            )
        db.commit()
    except StaleWikiRevisionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Wiki changed; retry source deletion") from exc
    except (WikiRepositoryError, SQLAlchemyError, OSError, ValueError) as exc:
        db.rollback()
        logger.exception("Source deletion failed", extra={"source_id": source_id})
        raise HTTPException(
            status_code=500,
            detail=(
                "Wiki cleanup completed, but source deletion failed; refresh and retry"
                if wiki_cleaned else "Source deletion failed; refresh and retry"
            ),
        ) from exc
    cleanup_source_objects(deletion, db)


@dataclass(frozen=True)
class SourceDeletion:
    source: Source
    revision_ids: tuple[str, ...]
    run_ids: tuple[str, ...]
    object_paths: frozenset[Path]
    repository: WikiRepository | None = None
    base_commit: str = ""
    wiki_changes: dict[str, str | None] = field(default_factory=dict)
    draft_updates: dict[str, dict[str, str | None]] = field(default_factory=dict)


def prepare_source_deletion(
    source_id: str,
    wiki_id: str,
    db: DbSession,
    settings: AppSettings,
    *,
    ignored_draft_ids: frozenset[str] = frozenset(),
    remove_references: bool = False,
) -> SourceDeletion:
    db.scalar(select(PersonalWiki.id).where(PersonalWiki.id == wiki_id).with_for_update())
    source = _owned_source(db, source_id, wiki_id)
    revisions = list(
        db.scalars(select(SourceRevision).where(SourceRevision.source_id == source.id))
    )
    revision_ids = {revision.id for revision in revisions}
    runs = _revision_runs(db, wiki_id, list(revision_ids))
    if any(run.status in {"queued", "parsing", "running", "publishing"} for run in runs):
        raise HTTPException(status_code=409, detail="Source is still being processed")
    repository = open_wiki_repository(
        settings.wiki_library_root(wiki_id),
        settings.legacy_wiki_library_root(wiki_id),
    )
    if not remove_references and any(
        _references_revision(repository.read_page(path), path, revision_ids)
        for path in repository.list_pages()
    ):
        raise HTTPException(
            status_code=409,
            detail="Source is referenced by Wiki pages; remove the references first",
        )
    drafts = list(db.scalars(select(Draft).where(
        Draft.wiki_id == wiki_id,
        Draft.id.not_in(ignored_draft_ids),
    )))
    if not remove_references and any(
        _references_revision(WikiPage.from_markdown(content), path, revision_ids)
        for draft in drafts
        for path, content in draft.files.items()
        if content is not None
    ):
        raise HTTPException(
            status_code=409, detail="Source is referenced by a draft; remove the draft first"
        )
    object_paths: set[Path] = set()
    for revision in revisions:
        object_path = (settings.storage_root / revision.object_path).resolve()
        if (
            not object_path.is_relative_to(settings.source_objects_root.resolve())
            or object_path.name != revision.sha256
        ):
            raise HTTPException(status_code=500, detail="Invalid source object path")
        object_paths.add(object_path)
    base_commit = repository.head()
    wiki_changes: dict[str, str | None] = {}
    draft_updates: dict[str, dict[str, str | None]] = {}
    if remove_references:
        published = {
            path: repository.read_page(path, base_commit).to_markdown()
            for path in repository.list_pages()
        }
        wiki_changes = remove_source_references(published, revision_ids)
        removed_paths = frozenset(
            path for path, content in wiki_changes.items() if content is None
        )
        linked_draft_ids = {
            draft_id for run in runs
            if isinstance((draft_id := (run.result_json or {}).get("draft_id")), str)
        }
        for draft in drafts:
            contents = {
                path: content for path, content in draft.files.items() if content is not None
            }
            changes = remove_source_references(
                contents, revision_ids, removed_paths=removed_paths,
            )
            if (
                not changes and draft.id not in linked_draft_ids
                and not removed_paths.intersection(draft.files)
            ):
                continue
            files = {
                path: changes.get(path, content)
                for path, content in draft.files.items()
                if changes.get(path, content) is not None or path not in removed_paths
            }
            # Removed draft-only pages need no tombstone in a future publication.
            files = {
                path: content for path, content in files.items()
                if content is not None or path in published and path not in removed_paths
            }
            if changes or draft.id in linked_draft_ids or files != draft.files:
                draft_updates[draft.id] = files
    return SourceDeletion(
        source=source,
        revision_ids=tuple(sorted(revision_ids)),
        run_ids=tuple(run.id for run in runs),
        object_paths=frozenset(object_paths),
        repository=repository if remove_references else None,
        base_commit=base_commit,
        wiki_changes=wiki_changes,
        draft_updates=draft_updates,
    )


def delete_run_checkpoints(db: DbSession, run_ids: tuple[str, ...]) -> None:
    if not run_ids:
        return
    inspector = inspect(db.connection())
    for name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
        if inspector.has_table(name):
            checkpoint_table = table(name, column("thread_id", String))
            db.execute(delete(checkpoint_table).where(checkpoint_table.c.thread_id.in_(run_ids)))


def delete_processing_runs(db: DbSession, run_ids: tuple[str, ...]) -> None:
    if not run_ids:
        return
    delete_run_checkpoints(db, run_ids)
    db.execute(delete(AgentEvent).where(AgentEvent.run_id.in_(run_ids)))
    db.execute(delete(AgentRun).where(AgentRun.id.in_(run_ids)))


def delete_source_records(deletion: SourceDeletion, user_id: str, db: DbSession) -> None:
    removed = set(deletion.revision_ids)
    delete_ids: list[str] = []
    for run_id in deletion.run_ids:
        run = db.get(AgentRun, run_id)
        remaining = (
            [revision for revision in run.source_revision_ids if revision not in removed]
            if run is not None else []
        )
        if not remaining:
            delete_ids.append(run_id)
            continue
        assert run is not None
        run.input_json = {
            **run.input_json,
            "source_revision_id": remaining[0],
            "source_revision_ids": remaining,
        }
        draft_id = (run.result_json or {}).get("draft_id")
        draft = db.get(Draft, draft_id) if isinstance(draft_id, str) else None
        if (
            draft is not None and run.input_json.get("batch")
            and draft.title.startswith("批量导入 · ")
        ):
            draft.title = f"批量导入 · {len(remaining)} 份资料"
        run.result_json = {
            **(run.result_json or {}), "summary": "",
            "conflicts": [
                f"pending_page:{path}"
                for path in (draft.files if draft is not None else {})
                if path.startswith("pending/")
            ],
        }
        delete_run_checkpoints(db, (run_id,))
        db.execute(delete(AgentEvent).where(
            AgentEvent.run_id == run_id,
            AgentEvent.payload["source_revision_id"].as_string().in_(removed),
        ))
    delete_processing_runs(db, tuple(delete_ids))
    db.execute(delete(SourceSpan).where(
        SourceSpan.source_revision_id.in_(deletion.revision_ids)
    ))
    source = deletion.source
    source.current_revision_id = None
    db.flush()
    db.execute(delete(SourceRevision).where(SourceRevision.source_id == source.id))
    db.delete(source)
    db.add(
        AuditLog(
            actor_id=user_id,
            action="source.deleted",
            resource_type="source",
            resource_id=source.id,
            details={"name": source.name},
        )
    )


def cleanup_source_objects(deletion: SourceDeletion, db: DbSession) -> None:
    for path in deletion.object_paths:
        still_used = db.scalar(
            select(SourceRevision.id).where(SourceRevision.sha256 == path.name).limit(1)
        )
        if still_used is None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.exception(
                    "Source deleted but object cleanup failed",
                    extra={"source_id": deletion.source.id},
                )
                raise HTTPException(
                    status_code=500, detail="Source deleted, but object cleanup failed"
                ) from exc


@router.get("/{source_id}/revisions", response_model=list[SourceRevisionView])
def list_source_revisions(
    source_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> list[SourceRevision]:
    source = db.get(Source, source_id)
    if source is None or source.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Source not found")
    return list(
        db.scalars(
            select(SourceRevision)
            .where(SourceRevision.source_id == source_id)
            .order_by(SourceRevision.created_at.desc())
        )
    )


@router.get("/revisions/{revision_id}", response_model=SourceRevisionView)
def get_source_revision(
    revision_id: str,
    wiki: CurrentWiki,
    db: DbSession,
) -> SourceRevision:
    revision = db.get(SourceRevision, revision_id)
    source = db.get(Source, revision.source_id) if revision is not None else None
    if revision is None or source is None or source.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Source revision not found")
    return revision


@router.get("/revisions/{revision_id}/spans", response_model=list[SourceSpanView])
def list_source_spans(
    revision_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> list[SourceSpan]:
    revision = db.get(SourceRevision, revision_id)
    source = db.get(Source, revision.source_id) if revision is not None else None
    if revision is None or source is None or source.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Source revision not found")
    return list(
        db.scalars(
            select(SourceSpan)
            .where(SourceSpan.source_revision_id == revision_id)
            .order_by(SourceSpan.ordinal)
        )
    )


@router.get("/revisions/{revision_id}/content")
def read_source_content(
    revision_id: str,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
    preview: bool = False,
) -> Response:
    revision = db.get(SourceRevision, revision_id)
    source = db.get(Source, revision.source_id) if revision is not None else None
    if revision is None or source is None or source.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Source revision not found")
    path = (settings.storage_root / revision.object_path).resolve()
    if (
        not path.is_relative_to(settings.source_objects_root.resolve())
        or path.name != revision.sha256
    ):
        raise HTTPException(status_code=500, detail="Invalid source object path")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Source object not found")
    if preview:
        if source.kind != SourceKind.WEBSITE:
            raise HTTPException(status_code=400, detail="该原件不支持网页正文预览。")
        final_url = revision.metadata_json.get("final_url", "")
        try:
            article = extract_web_article(
                path.read_bytes(), final_url if isinstance(final_url, str) else "",
                revision.media_type,
            )
        except WebContentError as exc:
            raise HTTPException(
                status_code=422, detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return Response(
            content=article.markdown, media_type="text/markdown",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return FileResponse(
        path,
        media_type=revision.media_type,
        filename=source.name,
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post(
    "",
    response_model=SourceIngestView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("source_upload"))],
)
def upload_source(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
) -> SourceIngestView:
    revision = _store_upload(user, wiki, db, settings, file)
    run = enqueue_ingested_revision(
        db, settings,
        source_revision_id=revision.id,
        created_by_id=user.id,
        wiki_id=wiki.id,
    )
    return _ingest_view(revision, run.id, run.status, run.error)


def _store_upload(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
    file: UploadFile,
) -> SourceRevision:
    kind = _validate_upload(file)
    store = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes)
    try:
        obj = store.put(file.file)
    except SourceTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    source = Source(
        wiki_id=wiki.id,
        name=Path(file.filename or obj.sha256).name,
        kind=kind,
        created_by_id=user.id,
    )
    db.add(source)
    db.flush()
    revision = SourceRevision(
        source_id=source.id,
        sha256=obj.sha256,
        object_path=str(obj.path.relative_to(settings.storage_root.resolve())),
        media_type=file.content_type or "application/octet-stream",
        size_bytes=obj.size_bytes,
    )
    db.add(revision)
    db.flush()
    source.current_revision_id = revision.id
    db.add(
        AuditLog(
            actor_id=user.id,
            action="source.uploaded",
            resource_type="source_revision",
            resource_id=revision.id,
            details={
                "name": source.name,
                "media_type": revision.media_type,
                "size_bytes": obj.size_bytes,
                "sha256": obj.sha256,
            },
        )
    )
    db.commit()
    db.refresh(revision)
    return revision


@router.post(
    "/batch",
    response_model=SourceBatchIngestView,
    status_code=201,
    dependencies=[Depends(require_feature("source_upload"))],
)
def upload_source_batch(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
    files: Annotated[list[UploadFile], File()],
) -> SourceBatchIngestView:
    if not files:
        raise HTTPException(status_code=422, detail="Import at least one file")
    records: list[tuple[str, SourceRevision | None, str | None]] = []
    revisions: list[str] = []
    for file in files:
        name = file.filename or "Unnamed file"
        try:
            if not file.file.read(1):
                raise HTTPException(status_code=422, detail="Empty files cannot be imported")
            file.file.seek(0)
            revision = _store_upload(user, wiki, db, settings, file)
        except HTTPException as exc:
            if exc.status_code not in {413, 415, 422}:
                raise
            records.append((name, None, str(exc.detail)))
        else:
            records.append((name, revision, None))
            revisions.append(revision.id)
    run = enqueue_ingested_batch(
        db, settings, source_revision_ids=revisions, created_by_id=user.id, wiki_id=wiki.id
    ) if revisions else None
    return SourceBatchIngestView(
        items=[
            SourceBatchItemView(
                file_name=name,
                revision=_ingest_view(revision, run.id, run.status, run.error)
                if revision is not None and run is not None else None,
                error=error,
            )
            for name, revision, error in records
        ],
        compilation_run_id=run.id if run else None,
        compilation_status=run.status if run else None,
        compilation_error=run.error if run else None,
    )


@router.post(
    "/{source_id}/revisions",
    response_model=SourceIngestView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("source_upload"))],
)
def upload_revision(
    source_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
) -> SourceIngestView:
    source = db.get(Source, source_id)
    if source is None or source.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Source not found")
    kind = _validate_upload(file)
    if kind is not source.kind:
        raise HTTPException(status_code=409, detail="Revision file type differs from source")
    store = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes)
    try:
        obj = store.put(file.file)
    except SourceTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    existing = db.scalar(
        select(SourceRevision).where(
            SourceRevision.source_id == source.id,
            SourceRevision.sha256 == obj.sha256,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="This source revision already exists")
    revision = SourceRevision(
        source_id=source.id,
        sha256=obj.sha256,
        object_path=str(obj.path.relative_to(settings.storage_root.resolve())),
        media_type=file.content_type or "application/octet-stream",
        size_bytes=obj.size_bytes,
    )
    db.add(revision)
    db.flush()
    source.current_revision_id = revision.id
    db.add(
        AuditLog(
            actor_id=user.id,
            action="source.revision_uploaded",
            resource_type="source_revision",
            resource_id=revision.id,
            details={
                "name": source.name,
                "source_id": source.id,
                "media_type": revision.media_type,
                "size_bytes": obj.size_bytes,
                "sha256": obj.sha256,
            },
        )
    )
    db.commit()
    db.refresh(revision)
    run = enqueue_ingested_revision(
        db,
        settings,
        source_revision_id=revision.id,
        created_by_id=user.id,
        wiki_id=wiki.id,
    )
    return _ingest_view(revision, run.id, run.status, run.error)


@router.post(
    "/web",
    response_model=SourceIngestView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("website_crawl"))],
)
def add_web_source(
    payload: WebsiteSourceCreate,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
) -> SourceIngestView:
    try:
        page = fetch_page(
            str(payload.url),
            max_bytes=min(settings.max_upload_bytes, 5 * 1024 * 1024),
        )
        try:
            article = extract_web_article(page.body, page.final_url, page.content_type)
        except WebContentError:
            if not settings.browser_service_url:
                raise
            page = BrowserClient(settings, user.id, wiki.id).render(str(payload.url))
            article = extract_web_article(page.body, page.final_url, page.content_type)
    except WebContentError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except (UnsafeSourceUrlError, httpx.HTTPError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        _, revision = store_web_source(
            db, settings, user_id=user.id, wiki_id=wiki.id,
            page=page, article=article, acquisition="web-import",
        )
    except WebContentError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": str(exc)},
        ) from exc
    db.commit()
    db.refresh(revision)
    run = enqueue_ingested_revision(
        db,
        settings,
        source_revision_id=revision.id,
        created_by_id=user.id,
        wiki_id=wiki.id,
    )
    return _ingest_view(revision, run.id, run.status, run.error)
