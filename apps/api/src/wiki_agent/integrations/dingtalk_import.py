"""Synchronous, create-only simulation import with a durable per-action claim."""

from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from wiki_agent.config import Settings
from wiki_agent.integrations.dingtalk_mock import (
    FIXTURE_VERSION,
    RUN_KIND,
    SOURCE_NOTE,
    MockDocument,
    get_document,
)
from wiki_agent.models import (
    AgentRun,
    AuditLog,
    Draft,
    DraftStatus,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
)
from wiki_agent.sources.storage import SourceObjectStore
from wiki_agent.wiki.repository import (
    PageExistsError,
    PublishBusyError,
    StaleWikiRevisionError,
    WikiRepository,
)
from wiki_agent.wiki.schema import Citation, PageKind, PageMetadata, WikiPage
from wiki_agent.wiki.validation import validate_citation_targets


def import_error(
    code: str, http: int, request_id: str | None = None, **known: object
) -> HTTPException:
    messages = {
        "integration_disabled": "模拟功能未启用；真实钉钉尚未实现",
        "document_not_found": "模拟文档不存在",
        "request_conflict": "请求ID已被使用，不能更改同一动作",
        "import_in_progress": "动作尚未完成，请查询原请求；不要再次导入",
        "wiki_revision_conflict": "Wiki版本已改变；本次未发布",
        "page_exists": "目标页面已存在；本次未覆盖",
        "publish_busy": "Wiki发布繁忙；本次未发布",
        "publication_outcome_unknown": "发布结果待核验；请查询原请求，不要重新导入",
        "published_state_not_recorded": "页面已发布但状态记录失败；请查询原请求，不要重新导入",
        "import_failed": "模拟导入失败；本次尚未发布",
    }
    detail = {"code": code, "message": messages[code], **known}
    if request_id:
        detail["request_id"] = request_id
    return HTTPException(http, detail=detail)


def page_for(document: MockDocument, request_id: str, revision_id: str, now: datetime) -> WikiPage:
    return WikiPage(
        metadata=PageMetadata(
            id=f"sim-dt-{request_id}",
            type=PageKind.SOURCE,
            title=document.title,
            created=now,
            updated=now,
            tags=["dingtalk-mock", "simulation"],
            citations={
                f"sim-{i + 1}": Citation(
                    source_revision_id=revision_id, span_id=f"{revision_id}:{i}"
                )
                for i in range(2)
            },
        ),
        body=document.canonical_text + "\n\n" + SOURCE_NOTE,
    )


def _result(run: AgentRun) -> dict[str, object]:
    return {
        "request_id": run.id,
        "state": run.status,
        "mode": "mock",
        "simulation": True,
        "real_connection": "not_implemented",
        "page_path": run.input_json["page_path"],
        **(run.result_json or {}),
    }


def _verified_commit(repo: WikiRepository, run: AgentRun) -> str | None:
    """Read-only residual check. Path alone never proves publication."""
    known = run.result_json or {}
    path = str(run.input_json["page_path"])
    revision = known.get("source_revision_id")
    if not revision:
        return None
    document = get_document(str(run.input_json["document_id"]))
    if run.input_json.get("fixture_version") != FIXTURE_VERSION:
        return None
    for item in repo.history(path):
        commit = item["commit"]
        page = repo.read_page(path, commit)
        expected = page_for(document, run.id, str(revision), page.metadata.created)
        if page != expected or repo.read_page(path) != expected:
            continue
        changed = repo._run(
            "diff-tree", "--no-commit-id", "--name-only", "-r", commit
        ).stdout.splitlines()
        if changed == [f"wiki/{path}"]:
            return commit
    return None


def query_import(
    db: Session, repo: WikiRepository, user_id: str, request_id: str
) -> dict[str, object]:
    run = db.get(AgentRun, request_id)
    if run is None or run.kind != RUN_KIND or run.created_by_id != user_id:
        raise HTTPException(404, detail="Import not found")
    result = _result(run)
    if run.status != "published":
        try:
            commit = _verified_commit(repo, run)
        except Exception:
            commit = None
        if commit:
            result.update(published=True, published_commit=commit)
        elif run.status in {"publishing", "outcome_unknown"}:
            result["published"] = None
        else:
            result["published"] = False
    return result


def _mark_failure(db: Session, request_id: str, code: str, state: str) -> None:
    db.rollback()
    try:
        run = db.get(AgentRun, request_id)
        if run is not None:
            run.status, run.error = state, code
            db.add(
                AuditLog(
                    actor_id=run.created_by_id,
                    action="dingtalk_mock.failed",
                    resource_type="agent_run",
                    resource_id=request_id,
                    details={"mode": "mock", "code": code},
                )
            )
            db.commit()
    except Exception:
        # The already committed publishing claim remains conservative if DB is unavailable.
        db.rollback()


def import_document(
    db: Session,
    repo: WikiRepository,
    settings: Settings,
    user: User,
    document_id: str,
    request_id: str,
) -> tuple[dict[str, object], bool]:
    if settings.dingtalk_mode != "mock":
        raise import_error("integration_disabled", 409)
    try:
        document = get_document(document_id)
    except KeyError:
        raise import_error("document_not_found", 404) from None
    user_id, author_name, author_email = user.id, user.display_name, user.email
    path = f"sources/dingtalk-mock-{request_id}.md"
    inputs = {
        "document_id": document_id,
        "fixture_version": FIXTURE_VERSION,
        "mode": "mock",
        "simulation": True,
        "page_path": path,
    }
    run = AgentRun(
        id=request_id, created_by_id=user_id, kind=RUN_KIND, status="running", input_json=inputs
    )
    db.add(run)
    try:
        db.commit()  # Atomic PK claim; no check-then-insert race.
    except IntegrityError:
        db.rollback()
        existing = db.get(AgentRun, request_id)
        if (
            existing is None
            or existing.kind != RUN_KIND
            or existing.created_by_id != user_id
            or existing.input_json != inputs
        ):
            raise import_error("request_conflict", 409, request_id) from None
        if existing.status == "published":
            return _result(existing), False
        known = query_import(db, repo, user_id, request_id)
        raise import_error(
            "import_in_progress",
            409,
            request_id,
            **{k: v for k, v in known.items() if k != "request_id"},
        ) from None
    except Exception:
        db.rollback()
        raise import_error("import_failed", 500, request_id, published=False) from None

    try:
        obj = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes).put(
            BytesIO(document.canonical_text.encode("utf-8"))
        )
        source_id, revision_id, draft_id = str(uuid4()), str(uuid4()), str(uuid4())
        now = datetime.now(UTC)
        page = page_for(document, request_id, revision_id, now)
        base = repo.head()
        source = Source(
            id=source_id, name=document.title, kind=SourceKind.TEXT, created_by_id=user_id
        )
        db.add(source)
        db.flush()
        db.add(
            SourceRevision(
                id=revision_id,
                source_id=source_id,
                sha256=obj.sha256,
                object_path=str(obj.path.relative_to(settings.storage_root.resolve())),
                media_type="text/plain",
                size_bytes=obj.size_bytes,
                status="parsed",
                metadata_json={
                    "origin": "dingtalk_mock",
                    "simulation": True,
                    "fixture_version": FIXTURE_VERSION,
                    "document_id": document_id,
                    "request_id": request_id,
                    "source_title": document.title,
                },
            )
        )
        db.flush()
        source.current_revision_id = revision_id
        db.add_all(
            [
                SourceSpan(
                    id=f"{revision_id}:{i}",
                    source_revision_id=revision_id,
                    ordinal=i,
                    content=text,
                    locator={
                        "origin": "dingtalk_mock",
                        "document_id": document_id,
                        "paragraph": i + 1,
                    },
                )
                for i, text in enumerate(document.canonical_text.split("\n\n"))
            ]
        )
        draft = Draft(
            id=draft_id,
            created_by_id=user_id,
            base_commit=base,
            files={path: page.to_markdown()},
            title=document.title,
        )
        db.add(draft)
        run.status = "prepared"
        run.result_json = {
            "page_path": path,
            "page_id": page.metadata.id,
            "source_revision_id": revision_id,
            "draft_id": draft_id,
            "index_status": "deferred",
        }
        db.flush()
        validate_citation_targets(draft.files, db)
        db.commit()
        run.status = "publishing"
        db.commit()
    except Exception:
        _mark_failure(db, request_id, "import_failed", "failed")
        raise import_error("import_failed", 500, request_id, published=False) from None

    try:
        published = repo.publish(
            {path: page.to_markdown()},
            base_commit=base,
            author_name=author_name,
            author_email=author_email,
            message=f"Import simulated DingTalk document {document_id}",
            create_only=True,
        )
        if published.changed_paths != (path,) or _verified_commit(repo, run) != published.commit:
            raise RuntimeError("Publication readback mismatch")
    except (StaleWikiRevisionError, PageExistsError, PublishBusyError) as exc:
        code = {
            StaleWikiRevisionError: "wiki_revision_conflict",
            PageExistsError: "page_exists",
            PublishBusyError: "publish_busy",
        }[type(exc)]
        _mark_failure(db, request_id, code, "failed")
        raise import_error(code, 409, request_id, published=False) from None
    except Exception:
        try:
            commit = _verified_commit(repo, run)
        except Exception:
            commit = None
        if not commit:
            _mark_failure(db, request_id, "publication_outcome_unknown", "outcome_unknown")
            raise import_error(
                "publication_outcome_unknown", 503, request_id, published=None, page_path=path
            ) from None
        # An exception after ff-merge may still have produced the exact page; finalize once.
        from wiki_agent.wiki.repository import PublishResult

        published = PublishResult(commit=commit, changed_paths=(path,))

    try:
        draft.status, draft.published_commit = DraftStatus.PUBLISHED, published.commit
        run.status = "published"
        run.result_json = {**(run.result_json or {}), "published_commit": published.commit}
        db.add(
            AuditLog(
                actor_id=user_id,
                action="dingtalk_mock.published",
                resource_type="agent_run",
                resource_id=request_id,
                details={
                    "mode": "mock",
                    "simulation": True,
                    "document_id": document_id,
                    "source_revision_id": revision_id,
                    "page_path": path,
                    "commit": published.commit,
                },
            )
        )
        db.commit()
    except Exception:
        db.rollback()
        raise import_error(
            "published_state_not_recorded",
            503,
            request_id,
            published=True,
            page_path=path,
            published_commit=published.commit,
        ) from None
    return _result(run), True
