from collections.abc import Generator
from contextlib import contextmanager
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any, cast
from urllib.parse import urlparse

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from wiki_agent.config import Settings, get_settings
from wiki_agent.db import SessionLocal
from wiki_agent.llm import ModelFactory, WikiCompiler, WikiVerifier
from wiki_agent.models import (
    AgentEvent,
    AgentRun,
    AuditLog,
    Draft,
    DraftStatus,
    FeatureFlag,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
)
from wiki_agent.publishing import ChangeRisk, PublicationPolicy, evaluate_change_risk
from wiki_agent.sources import SourceObjectStore
from wiki_agent.sources.crawler import CrawlPolicy, CrawlRequest, crawl_site
from wiki_agent.sources.parser import DocumentParser
from wiki_agent.wiki import WikiPage, WikiRepository
from wiki_agent.wiki.indexer import WikiIndexer
from wiki_agent.wiki.validation import validate_citation_targets
from wiki_agent.worker import celery_app
from wiki_agent.workflows import (
    CompilationServices,
    CompilationState,
    CompiledPages,
    VerificationOutcome,
    build_compilation_graph,
)


@celery_app.task(name="wiki_agent.compile_source", bind=True)  # type: ignore[untyped-decorator]
def compile_source(self: object, run_id: str) -> dict[str, str]:
    with SessionLocal() as db:
        return _compile_source(db, run_id)


@celery_app.task(name="wiki_agent.crawl_site", bind=True)  # type: ignore[untyped-decorator]
def crawl_site_task(self: object, run_id: str) -> dict[str, object]:
    with SessionLocal() as db:
        return _crawl_site(db, run_id)


@celery_app.task(name="wiki_agent.parse_source", bind=True)  # type: ignore[untyped-decorator]
def parse_source_revision_task(self: object, run_id: str) -> dict[str, object]:
    with SessionLocal() as db:
        return _parse_source_run(db, run_id)


@celery_app.task(  # type: ignore[untyped-decorator]
    name="wiki_agent.prepare_compilation_source",
    bind=True,
)
def prepare_compilation_source_task(self: object, run_id: str) -> dict[str, object]:
    with SessionLocal() as db:
        return _prepare_compilation_source(db, run_id)


def _parse_source_run(db: Session, run_id: str) -> dict[str, object]:
    settings = get_settings()
    run = db.get(AgentRun, run_id)
    if run is None:
        raise ValueError(f"agent run {run_id} does not exist")
    run.status = "running"
    db.add(AgentEvent(run_id=run.id, kind="run.started", payload={}))
    db.commit()
    try:
        revision_id = str(run.input_json["source_revision_id"])
        revision = db.get(SourceRevision, revision_id)
        if revision is None:
            raise ValueError("source revision does not exist")
        source = db.get(Source, revision.source_id)
        if source is None:
            raise ValueError("source does not exist")
        marked_sections = _parse_revision(db, run, revision, source, settings)
        run.status = "completed"
        run.result_json = {
            "source_revision_id": revision.id,
            "span_count": len(marked_sections),
        }
        db.commit()
        return {"run_id": run.id, **run.result_json}
    except Exception as exc:
        _fail_run(db, run_id, exc)
        raise


def _prepare_compilation_source(db: Session, run_id: str) -> dict[str, object]:
    settings = get_settings()
    run = db.get(AgentRun, run_id)
    if run is None:
        raise ValueError(f"agent run {run_id} does not exist")
    try:
        revision_id = str(run.input_json["source_revision_id"])
        revision = db.get(SourceRevision, revision_id)
        if revision is None:
            raise ValueError("source revision does not exist")
        existing_spans = db.scalar(
            select(SourceSpan.id)
            .where(SourceSpan.source_revision_id == revision.id)
            .limit(1)
        )
        if revision.status == "parsed" and existing_spans is not None:
            return {"run_id": run.id, "source_revision_id": revision.id, "cached": True}
        source = db.get(Source, revision.source_id)
        if source is None:
            raise ValueError("source does not exist")
        run.status = "parsing"
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="source.parse_started",
                payload={"source_revision_id": revision.id},
            )
        )
        db.commit()
        marked_sections = _parse_revision(db, run, revision, source, settings)
        run.status = "queued"
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="source.ready_for_compilation",
                payload={
                    "source_revision_id": revision.id,
                    "span_count": len(marked_sections),
                },
            )
        )
        db.commit()
        return {
            "run_id": run.id,
            "source_revision_id": revision.id,
            "cached": False,
        }
    except Exception as exc:
        _fail_run(db, run_id, exc)
        raise


def _crawl_site(db: Session, run_id: str) -> dict[str, object]:
    settings = get_settings()
    run = db.get(AgentRun, run_id)
    if run is None:
        raise ValueError(f"agent run {run_id} does not exist")
    run.status = "running"
    db.add(AgentEvent(run_id=run.id, kind="run.started", payload={}))
    db.commit()
    try:
        payload = CrawlRequest.model_validate(run.input_json)
        result = crawl_site(
            str(payload.url),
            CrawlPolicy(
                max_depth=payload.max_depth,
                max_pages=payload.max_pages,
                include_patterns=tuple(payload.include_patterns),
                exclude_patterns=tuple(payload.exclude_patterns),
            ),
        )
        store = SourceObjectStore(settings.source_objects_root, settings.max_upload_bytes)
        revision_ids: list[str] = []
        for page in result.pages:
            obj = store.put(BytesIO(page.body))
            source = Source(
                name=page.final_url,
                kind=SourceKind.WEBSITE,
                created_by_id=run.created_by_id,
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
                    "host": urlparse(page.final_url).hostname,
                },
            )
            db.add(revision)
            db.flush()
            source.current_revision_id = revision.id
            revision_ids.append(revision.id)
        run.status = "completed"
        run.result_json = {
            "source_revision_ids": revision_ids,
            "errors": [
                {"url": error.url, "error": error.error} for error in result.errors
            ],
        }
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="crawl.completed",
                payload={
                    "page_count": len(revision_ids),
                    "error_count": len(result.errors),
                },
            )
        )
        db.commit()
        return {
            "run_id": run.id,
            "source_revision_ids": revision_ids,
        }
    except Exception as exc:
        _fail_run(db, run_id, exc)
        raise


def _compile_source(db: Session, run_id: str) -> dict[str, str]:
    settings = get_settings()
    run = db.get(AgentRun, run_id)
    if run is None:
        raise ValueError(f"agent run {run_id} does not exist")
    existing_draft_id = (
        str(run.result_json["draft_id"])
        if run.result_json is not None and "draft_id" in run.result_json
        else None
    )
    if existing_draft_id is not None:
        return {"run_id": run.id, "draft_id": existing_draft_id}
    run.status = "running"
    db.add(AgentEvent(run_id=run.id, kind="run.started", payload={}))
    db.commit()
    try:
        revision_id = str(run.input_json["source_revision_id"])
        profile_id = str(run.input_json["model_profile_id"])
        revision = db.get(SourceRevision, revision_id)
        if revision is None:
            raise ValueError("source revision does not exist")
        source = db.get(Source, revision.source_id)
        if source is None:
            raise ValueError("source does not exist")
        repository = WikiRepository(settings.knowledge_root)
        services = _build_compilation_services(
            db,
            run,
            settings,
            profile_id=profile_id,
            repository=repository,
        )
        with _compilation_checkpointer(settings) as checkpointer:
            graph = build_compilation_graph(services, checkpointer)
            result = cast(
                CompilationState,
                graph.invoke(
                    {
                        "source_revision_id": revision_id,
                        "requested_policy": str(
                            run.input_json["publication_policy"]
                        ),
                    },
                    {"configurable": {"thread_id": run.id}},
                ),
            )
        files = result.get("files")
        base_commit = result.get("base_commit")
        if files is None or base_commit is None:
            raise ValueError("compilation workflow did not produce a draft")
        risk_violations = result.get("risk_violations", [])
        draft = Draft(
            created_by_id=run.created_by_id,
            base_commit=base_commit,
            files=files,
            title=f"Compile {source.name}",
        )
        db.add(draft)
        db.flush()
        should_auto_publish = result.get("status") == "approved"
        run.status = "publishing" if should_auto_publish else "awaiting_review"
        run.result_json = {
            "draft_id": draft.id,
            "summary": result.get("summary", ""),
            "conflicts": result.get("conflicts", []),
            "verification_issues": result.get("verification_issues", []),
            "risk_violations": risk_violations,
            "requested_policy": run.input_json["publication_policy"],
            "publication": "auto_publish" if should_auto_publish else "manual_review",
        }
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="draft.ready",
                payload={
                    "draft_id": draft.id,
                    "conflicts": result.get("conflicts", []),
                },
            )
        )
        db.add(
            AuditLog(
                actor_id=run.created_by_id,
                action="agent.compile_completed",
                resource_type="agent_run",
                resource_id=run.id,
                details={"draft_id": draft.id},
            )
        )
        db.commit()
        if should_auto_publish:
            creator = db.get(User, run.created_by_id)
            if creator is None:
                raise ValueError("draft creator does not exist")
            publish_result = repository.publish(
                files,
                base_commit=base_commit,
                author_name=creator.display_name,
                author_email=creator.email,
                message=f"Auto-publish: {draft.title}",
            )
            draft.status = DraftStatus.PUBLISHED
            draft.published_commit = publish_result.commit
            run.status = "completed"
            run.result_json = {
                **(run.result_json or {}),
                "published_commit": publish_result.commit,
            }
            db.add(
                AgentEvent(
                    run_id=run.id,
                    kind="draft.auto_published",
                    payload={
                        "draft_id": draft.id,
                        "commit": publish_result.commit,
                    },
                )
            )
            db.commit()
            try:
                factory = ModelFactory(db, settings)
                embedding_profile_id = factory.assigned_profile_id("embedding")
                WikiIndexer(repository).rebuild(
                    db,
                    factory.embeddings(embedding_profile_id)
                    if embedding_profile_id is not None
                    else None,
                )
            except Exception as indexing_error:
                run.status = "published_index_failed"
                run.error = f"{type(indexing_error).__name__}: {indexing_error}"
                db.add(
                    AgentEvent(
                        run_id=run.id,
                        kind="index.failed",
                        payload={"error": run.error, "commit": publish_result.commit},
                    )
                )
                db.commit()
                return {"run_id": run.id, "draft_id": draft.id}
        return {"run_id": run.id, "draft_id": draft.id}
    except Exception as exc:
        _fail_run(db, run_id, exc)
        raise


def _build_compilation_services(
    db: Session,
    run: AgentRun,
    settings: Settings,
    *,
    profile_id: str,
    repository: WikiRepository,
) -> CompilationServices:
    schema_path = settings.schema_root / "AGENTS.md"

    def load_source(revision_id: str) -> str:
        revision = db.get(SourceRevision, revision_id)
        if revision is None:
            raise ValueError("source revision does not exist")
        spans = list(
            db.scalars(
                select(SourceSpan)
                .where(SourceSpan.source_revision_id == revision_id)
                .order_by(SourceSpan.ordinal)
            )
        )
        if revision.status != "parsed" or not spans:
            raise ValueError("source revision must be parsed before compilation")
        return "\n\n".join(
            f'<SOURCE_SPAN id="{span.id}">\n{span.content}\n</SOURCE_SPAN>'
            for span in spans
        )

    def compile_pages(source_markdown: str, revision_id: str) -> CompiledPages:
        factory = ModelFactory(db, settings)
        proposal = WikiCompiler(factory.chat(profile_id)).compile(
            source_revision_id=revision_id,
            source_markdown=source_markdown,
            schema=schema_path.read_text(encoding="utf-8"),
            existing_pages=repository.list_pages(),
        )
        return CompiledPages(
            files={item.path: item.content for item in proposal.files},
            summary=proposal.summary,
            conflicts=proposal.conflicts,
        )

    def link_pages(
        files: dict[str, str | None], base_commit: str
    ) -> dict[str, str | None]:
        repository.validate_changes(files, base_commit=base_commit)
        return files

    def detect_conflicts(
        files: dict[str, str | None], proposed_conflicts: list[str]
    ) -> list[str]:
        pending_pages = [
            f"pending_page:{path}" for path in files if path.startswith("pending/")
        ]
        return sorted(set([*proposed_conflicts, *pending_pages]))

    def verify(
        revision_id: str,
        files: dict[str, str | None],
        conflicts: list[str],
        base_commit: str,
    ) -> VerificationOutcome:
        validate_citation_targets(files, db)
        revision = db.get(SourceRevision, revision_id)
        if revision is None:
            raise ValueError("source revision does not exist")
        source = db.get(Source, revision.source_id)
        if source is None:
            raise ValueError("source does not exist")
        factory = ModelFactory(db, settings)
        verifier_profile_id = factory.assigned_profile_id("planning") or profile_id
        verification = WikiVerifier(factory.chat(verifier_profile_id)).verify(
            files,
            schema_path.read_text(encoding="utf-8"),
        )
        existing_paths = set(repository.list_pages())
        change_ratios = [
            _change_ratio(repository, path, content, existing_paths)
            for path, content in files.items()
        ]
        risk = ChangeRisk(
            changed_pages=len(files),
            largest_changed_ratio=max(change_ratios, default=0),
            new_claims=sum(
                len(WikiPage.from_markdown(content).metadata.citations)
                for content in files.values()
                if content is not None
            ),
            minimum_source_trust=source.trust_level,
            citations_complete=True,
            has_conflicts=bool(conflicts),
            has_deletions=any(content is None for content in files.values()),
            has_renames=False,
            has_entity_merges=False,
            schema_valid=True,
            links_valid=True,
            verifier_passed=verification.passed,
            workflow_degraded=False,
            base_commit_current=repository.head() == base_commit,
        )
        auto_publish_flag = db.get(FeatureFlag, "auto_publish")
        policy = PublicationPolicy.from_mapping(
            auto_publish_flag.config if auto_publish_flag is not None else {}
        )
        return VerificationOutcome(
            issues=verification.issues,
            risk_violations=evaluate_change_risk(risk, policy),
        )

    return CompilationServices(
        load_source=load_source,
        current_revision=repository.head,
        compile_pages=compile_pages,
        link_pages=link_pages,
        detect_conflicts=detect_conflicts,
        verify=verify,
    )


@contextmanager
def _compilation_checkpointer(
    settings: Settings,
) -> Generator[BaseCheckpointSaver[Any] | None]:
    if not settings.database_url.startswith("postgresql"):
        yield None
        return
    connection_string = settings.database_url.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with PostgresSaver.from_conn_string(connection_string) as checkpointer:
        checkpointer.setup()
        yield checkpointer


def _change_ratio(
    repository: WikiRepository,
    path: str,
    content: str | None,
    existing_paths: set[str],
) -> float:
    if content is None or path not in existing_paths:
        return 1.0
    previous = repository.read_page(path).to_markdown()
    return 1.0 - SequenceMatcher(None, previous, content).ratio()


def _parse_revision(
    db: Session,
    run: AgentRun,
    revision: SourceRevision,
    source: Source,
    settings: Settings,
) -> list[str]:
    object_path = (settings.storage_root / revision.object_path).resolve()
    if not object_path.is_relative_to(settings.source_objects_root.resolve()):
        raise ValueError("source object path escapes the configured source store")
    parsed = DocumentParser().parse(object_path, source.kind)
    factory = ModelFactory(db, settings)
    embedding_profile_id = factory.assigned_profile_id("embedding")
    section_vectors = (
        factory.embeddings(embedding_profile_id).embed_documents(
            [section.text for section in parsed.sections]
        )
        if embedding_profile_id is not None
        else [None] * len(parsed.sections)
    )
    if len(section_vectors) != len(parsed.sections):
        raise ValueError("embedding service returned an unexpected number of vectors")
    db.execute(delete(SourceSpan).where(SourceSpan.source_revision_id == revision.id))
    marked_sections: list[str] = []
    for section, vector in zip(parsed.sections, section_vectors, strict=True):
        span_id = f"{revision.id}:{section.ordinal}"
        db.add(
            SourceSpan(
                id=span_id,
                source_revision_id=revision.id,
                ordinal=section.ordinal,
                heading=section.heading,
                content=section.text,
                locator=section.locator,
                embedding=vector,
            )
        )
        marked_sections.append(
            f'<SOURCE_SPAN id="{span_id}">\n{section.text}\n</SOURCE_SPAN>'
        )
    revision.status = "parsed"
    db.add(
        AgentEvent(
            run_id=run.id,
            kind="source.parsed",
            payload={
                "source_revision_id": revision.id,
                "span_count": len(parsed.sections),
                "parser": parsed.parser,
            },
        )
    )
    db.flush()
    db.commit()
    return marked_sections


def _fail_run(db: Session, run_id: str, error: Exception) -> None:
    db.rollback()
    failed_run = db.get(AgentRun, run_id)
    if failed_run is None:
        return
    failed_run.status = "failed"
    failed_run.error = f"{type(error).__name__}: {error}"
    db.add(
        AgentEvent(
            run_id=failed_run.id,
            kind="run.failed",
            payload={"error": failed_run.error},
        )
    )
    db.commit()
