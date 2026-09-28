from io import BytesIO
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select

from wiki_agent.api.dependencies import AppSettings, CurrentUser, DbSession, require_feature
from wiki_agent.api.schemas import (
    SourceRevisionView,
    SourceSpanView,
    SourceView,
    WebsiteSourceCreate,
)
from wiki_agent.models import AuditLog, Source, SourceKind, SourceRevision, SourceSpan
from wiki_agent.sources import SourceObjectStore
from wiki_agent.sources.fetcher import UnsafeSourceUrlError, fetch_page
from wiki_agent.sources.storage import SourceTooLargeError

router = APIRouter(prefix="/sources", tags=["sources"])

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


@router.get("", response_model=list[SourceView])
def list_sources(user: CurrentUser, db: DbSession) -> list[Source]:
    return list(db.scalars(select(Source).order_by(Source.created_at.desc())))


@router.get("/{source_id}/revisions", response_model=list[SourceRevisionView])
def list_source_revisions(
    source_id: str,
    user: CurrentUser,
    db: DbSession,
) -> list[SourceRevision]:
    if db.get(Source, source_id) is None:
        raise HTTPException(status_code=404, detail="Source not found")
    return list(
        db.scalars(
            select(SourceRevision)
            .where(SourceRevision.source_id == source_id)
            .order_by(SourceRevision.created_at.desc())
        )
    )


@router.get("/revisions/{revision_id}/spans", response_model=list[SourceSpanView])
def list_source_spans(
    revision_id: str,
    user: CurrentUser,
    db: DbSession,
) -> list[SourceSpan]:
    if db.get(SourceRevision, revision_id) is None:
        raise HTTPException(status_code=404, detail="Source revision not found")
    return list(
        db.scalars(
            select(SourceSpan)
            .where(SourceSpan.source_revision_id == revision_id)
            .order_by(SourceSpan.ordinal)
        )
    )


@router.post(
    "",
    response_model=SourceRevisionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("source_upload"))],
)
def upload_source(
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
) -> SourceRevision:
    kind = _validate_upload(file)
    store = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes)
    try:
        obj = store.put(file.file)
    except SourceTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    source = Source(name=Path(file.filename or obj.sha256).name, kind=kind, created_by_id=user.id)
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
            details={"sha256": obj.sha256, "size_bytes": obj.size_bytes},
        )
    )
    db.commit()
    db.refresh(revision)
    return revision


@router.post(
    "/{source_id}/revisions",
    response_model=SourceRevisionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("source_upload"))],
)
def upload_revision(
    source_id: str,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
) -> SourceRevision:
    source = db.get(Source, source_id)
    if source is None:
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
            details={"source_id": source.id, "sha256": obj.sha256},
        )
    )
    db.commit()
    db.refresh(revision)
    return revision


@router.post(
    "/web",
    response_model=SourceRevisionView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("website_crawl"))],
)
def add_web_source(
    payload: WebsiteSourceCreate,
    user: CurrentUser,
    db: DbSession,
    settings: AppSettings,
) -> SourceRevision:
    try:
        page = fetch_page(
            str(payload.url),
            max_bytes=min(settings.max_upload_bytes, 5 * 1024 * 1024),
        )
    except (UnsafeSourceUrlError, httpx.HTTPError, OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes)
    obj = store.put(BytesIO(page.body))
    source = Source(
        name=page.final_url,
        kind=SourceKind.WEBSITE,
        created_by_id=user.id,
    )
    db.add(source)
    db.flush()
    revision = SourceRevision(
        source_id=source.id,
        sha256=obj.sha256,
        object_path=str(obj.path.relative_to(settings.storage_root.resolve())),
        media_type=page.content_type,
        size_bytes=obj.size_bytes,
        metadata_json={
            "requested_url": page.requested_url,
            "final_url": page.final_url,
        },
    )
    db.add(revision)
    db.flush()
    source.current_revision_id = revision.id
    db.add(
        AuditLog(
            actor_id=user.id,
            action="source.web_added",
            resource_type="source_revision",
            resource_id=revision.id,
            details={"url": page.final_url, "sha256": obj.sha256},
        )
    )
    db.commit()
    db.refresh(revision)
    return revision
