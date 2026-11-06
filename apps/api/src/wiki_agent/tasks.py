from collections.abc import Generator
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import delete, or_, select
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
from wiki_agent.sources.article import WebContentError, public_article_url
from wiki_agent.sources.browser import BrowserClient
from wiki_agent.sources.crawler import CrawlError, CrawlPolicy, CrawlRequest, crawl_site
from wiki_agent.sources.parser import DocumentParser
from wiki_agent.sources.web_import import store_web_source
from wiki_agent.wiki import WikiPage, WikiRepository, open_wiki_repository
from wiki_agent.wiki.indexer import WikiIndexer
from wiki_agent.wiki.linking import complete_bidirectional_links
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
    name="wiki_agent.backfill_source_embeddings",
    bind=True,
)
def backfill_source_embeddings_task(self: object, run_id: str) -> dict[str, object]:
    with SessionLocal() as db:
        return _backfill_source_embeddings(db, run_id)


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
        if source is None or source.wiki_id != run.wiki_id:
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


def _backfill_source_embeddings(db: Session, run_id: str) -> dict[str, object]:
    settings = get_settings()
    run = db.get(AgentRun, run_id)
    if run is None:
        raise ValueError(f"agent run {run_id} does not exist")
    profile_id = run.input_json.get("embedding_profile_id")
    batch_size = run.input_json.get("batch_size", 64)
    if not isinstance(profile_id, str) or not profile_id:
        raise ValueError("embedding_profile_id is required")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    run.status = "running"
    db.add(AgentEvent(run_id=run.id, kind="run.started", payload={}))
    db.commit()
    try:
        embeddings = ModelFactory(db, settings, run.created_by_id).embeddings(profile_id)
        db.commit()
        updated_count = 0
        while True:
            spans = list(
                db.scalars(
                    select(SourceSpan)
                    .join(
                        SourceRevision,
                        SourceRevision.id == SourceSpan.source_revision_id,
                    )
                    .join(Source, Source.id == SourceRevision.source_id)
                    .where(
                        Source.wiki_id == run.wiki_id,
                        or_(
                            SourceSpan.embedding.is_(None),
                            SourceSpan.embedding_profile_id.is_(None),
                            SourceSpan.embedding_profile_id != profile_id,
                        ),
                    )
                    .order_by(SourceSpan.id)
                    .limit(batch_size)
                )
            )
            if not spans:
                break
            contents = [span.content for span in spans]
            db.commit()
            vectors = embeddings.embed_documents(contents)
            if len(vectors) != len(spans):
                raise ValueError("embedding service returned an unexpected number of vectors")
            for span, vector in zip(spans, vectors, strict=True):
                span.embedding = vector
                span.embedding_profile_id = profile_id
            updated_count += len(spans)
            db.add(
                AgentEvent(
                    run_id=run.id,
                    kind="embeddings.batch_completed",
                    payload={
                        "batch_count": len(spans),
                        "updated_count": updated_count,
                    },
                )
            )
            db.commit()
        run.status = "completed"
        run.result_json = {
            "embedding_profile_id": profile_id,
            "updated_span_count": updated_count,
        }
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="embeddings.backfill_completed",
                payload=run.result_json,
            )
        )
        db.add(
            AuditLog(
                actor_id=run.created_by_id,
                action="source_embeddings.backfilled",
                resource_type="personal_wiki",
                resource_id=run.wiki_id,
                details=run.result_json,
            )
        )
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
        revision_ids = run.source_revision_ids
        if not revision_ids:
            raise ValueError("compilation has no source revisions")
        cached = True
        for revision_id in revision_ids:
            revision = db.get(SourceRevision, revision_id)
            if revision is None:
                raise ValueError("source revision does not exist")
            source = db.get(Source, revision.source_id)
            if source is None or source.wiki_id != run.wiki_id:
                raise ValueError("source does not exist in the current Wiki")
            existing_spans = db.scalar(
                select(SourceSpan.id).where(SourceSpan.source_revision_id == revision.id).limit(1)
            )
            if revision.status == "parsed" and existing_spans is not None:
                continue
            cached = False
            run.status = "parsing"
            db.add(AgentEvent(
                run_id=run.id, kind="source.parse_started",
                payload={"source_revision_id": revision.id},
            ))
            db.commit()
            marked_sections = _parse_revision(db, run, revision, source, settings)
            db.add(AgentEvent(
                run_id=run.id, kind="source.ready_for_compilation",
                payload={"source_revision_id": revision.id, "span_count": len(marked_sections)},
            ))
            db.commit()
        run.status = "queued"
        db.commit()
        return {
            "run_id": run.id,
            "source_revision_id": revision_ids[0],
            "source_revision_ids": revision_ids,
            "cached": cached,
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
        browser = BrowserClient(settings, run.created_by_id, run.wiki_id)
        result = crawl_site(
            str(payload.url),
            CrawlPolicy(
                max_depth=payload.max_depth,
                max_pages=payload.max_pages,
                include_patterns=tuple(payload.include_patterns),
                exclude_patterns=tuple(payload.exclude_patterns),
            ),
            render_page=browser.render if settings.browser_service_url else None,
        )
        revision_ids: list[str] = []
        errors = list(result.errors)
        for page in result.pages:
            try:
                _, revision = store_web_source(
                    db, settings, user_id=run.created_by_id, wiki_id=run.wiki_id,
                    page=page, article=page.article, acquisition=page.acquisition,
                )
            except WebContentError as exc:
                errors.append(CrawlError(page.final_url, str(exc), exc.code))
                continue
            revision_ids.append(revision.id)
        for error in errors:
            db.add(AuditLog(
                actor_id=run.created_by_id, action="source.web_failed",
                resource_type="agent_run", resource_id=run.id,
                details={"url": public_article_url(error.url), "error": error.error},
            ))
        run.result_json = {
            "source_revision_ids": revision_ids,
            "imported_count": len(revision_ids),
            "failed_count": len(errors),
            "browser_required": any(error.code in {
                "verification_required", "login_required", "empty_content",
                "browser_required", "browser_unavailable",
            } for error in errors),
            "errors": [
                {"url": error.url, "error": error.error, "code": error.code}
                for error in errors
            ],
        }
        if not revision_ids:
            db.commit()
            explanation = errors[0].error if errors else "没有提取到有效文章正文。"
            raise ValueError(f"没有导入有效正文。{explanation}")
        run.status = "completed_with_errors" if errors else "completed"
        db.add(
            AgentEvent(
                run_id=run.id,
                kind="crawl.completed",
                payload={
                    "page_count": len(revision_ids),
                    "error_count": len(errors),
                },
            )
        )
        db.commit()
        return {
            "run_id": run.id,
            "source_revision_ids": revision_ids,
            "status": run.status,
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
        revision_ids = run.source_revision_ids
        if not revision_ids:
            raise ValueError("compilation has no source revisions")
        revision_id = revision_ids[0]
        profile_id = str(run.input_json["model_profile_id"])
        revision = db.get(SourceRevision, revision_id)
        if revision is None:
            raise ValueError("source revision does not exist")
        source = db.get(Source, revision.source_id)
        if source is None or source.wiki_id != run.wiki_id:
            raise ValueError("source does not exist")
        repository = open_wiki_repository(
            settings.wiki_library_root(run.wiki_id),
            settings.legacy_wiki_library_root(run.wiki_id),
        )
        services = _build_compilation_services(
            db,
            run,
            settings,
            profile_id=profile_id,
            repository=repository,
        )
        # Setup runs migrations on a separate connection; do not hold a read transaction.
        db.commit()
        with _compilation_checkpointer(settings) as checkpointer:
            graph = build_compilation_graph(services, checkpointer)
            result = cast(
                CompilationState,
                graph.invoke(
                    {
                        "source_revision_id": revision_id,
                        "requested_policy": (
                            "manual_review" if run.input_json.get("batch")
                            else str(run.input_json["publication_policy"])
                        ),
                    },
                    {"configurable": {"thread_id": run.id}},
                ),
            )
        files = result.get("files")
        base_commit = result.get("base_commit")
        if files is None or base_commit is None:
            raise ValueError("compilation workflow did not produce a draft")
        owned_run = db.scalar(
            select(AgentRun.id).where(AgentRun.id == run_id).with_for_update()
        )
        if owned_run is None:
            raise ValueError("compilation run was removed")
        db.refresh(run)
        saved_draft_id = (run.result_json or {}).get("draft_id")
        if isinstance(saved_draft_id, str):
            db.commit()
            return {"run_id": run.id, "draft_id": saved_draft_id}
        risk_violations = result.get("risk_violations", [])
        draft = Draft(
            wiki_id=run.wiki_id,
            created_by_id=run.created_by_id,
            base_commit=base_commit,
            files=files,
            title=(
                f"批量导入 · {len(revision_ids)} 份资料"
                if run.input_json.get("batch") else f"Compile {source.name}"
            ),
        )
        db.add(draft)
        db.flush()
        should_auto_publish = result.get("status") == "approved" and not run.input_json.get("batch")
        run.status = "publishing" if should_auto_publish else "awaiting_review"
        run.result_json = {
            "draft_id": draft.id,
            "summary": result.get("summary", ""),
            "conflicts": result.get("conflicts", []),
            "verification_issues": result.get("verification_issues", []),
            "risk_violations": risk_violations,
            "requested_policy": run.input_json["publication_policy"],
            "publication": "auto_publish" if should_auto_publish else "manual_review",
            "source_revision_ids": revision_ids,
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
            author_name, author_email = creator.display_name, creator.local_commit_email
            db.commit()
            publish_result = repository.publish(
                files,
                base_commit=base_commit,
                author_name=author_name,
                author_email=author_email,
                message=f"Auto-publish: {draft.title}",
            )
            draft.status = DraftStatus.PUBLISHED
            draft.published_commit = publish_result.commit
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
                factory = ModelFactory(db, settings, run.created_by_id)
                embedding_profile_id = factory.assigned_profile_id("embedding")
                embeddings = (
                    factory.embeddings(embedding_profile_id)
                    if embedding_profile_id is not None
                    else None
                )
                db.commit()
                WikiIndexer(repository, run.wiki_id).rebuild(
                    db,
                    embeddings,
                    embedding_profile_id=embedding_profile_id,
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
            run.status = "completed"
            db.commit()
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
    schema_path = repository.root / "schema" / "AGENTS.md"
    revision_ids = run.source_revision_ids

    def source_text(revision_id: str) -> str:
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
        source_markdown = "\n\n".join(
            f'<SOURCE_SPAN id="{span.id}">\n{span.content}\n</SOURCE_SPAN>' for span in spans
        )
        db.commit()
        return source_markdown

    def load_source(revision_id: str) -> str:
        if not run.input_json.get("batch"):
            return source_text(revision_id)
        return "\n\n".join(
            f'<SOURCE_REVISION id="{member}">\n{source_text(member)}\n</SOURCE_REVISION>'
            for member in revision_ids
        )

    def compile_pages(source_markdown: str, revision_id: str) -> CompiledPages:
        factory = ModelFactory(db, settings, run.created_by_id)
        existing_pages = {
            path: repository.read_page(path).to_markdown() for path in repository.list_pages()
        }
        model = factory.chat(
            profile_id, timeout_seconds=settings.compilation_model_timeout_seconds
        )
        db.commit()
        compiler = WikiCompiler(model)
        files: dict[str, str | None] = {}
        summaries: list[str] = []
        conflicts: list[str] = []
        members = revision_ids if run.input_json.get("batch") else [revision_id]
        for member in members:
            text = source_text(member) if run.input_json.get("batch") else source_markdown
            proposal = compiler.compile(
                source_revision_id=member,
                source_markdown=text,
                schema=schema_path.read_text(encoding="utf-8"),
                existing_pages=existing_pages,
            )
            updates = {item.path: item.content for item in proposal.files}
            files.update(updates)
            for path, content in updates.items():
                if content is None:
                    existing_pages.pop(path, None)
                else:
                    existing_pages[path] = content
            summaries.append(proposal.summary)
            conflicts.extend(proposal.conflicts)
            db.add(AgentEvent(
                run_id=run.id, kind="source.compiled",
                payload={"source_revision_id": member, "completed_count": len(summaries)},
            ))
            db.commit()
        if run.input_json.get("batch"):
            missing = [member for member in members if not files.get(f"sources/{member}.md")]
            if missing:
                raise ValueError("Compilation batch must retain every source summary")
        return CompiledPages(
            files=files,
            summary="\n".join(summaries),
            conflicts=sorted(set(conflicts)),
        )

    def link_pages(files: dict[str, str | None], base_commit: str) -> dict[str, str | None]:
        existing_pages = {
            path: repository.read_page(path, revision=base_commit).to_markdown()
            for path in repository.list_pages(revision=base_commit)
        }
        linked = complete_bidirectional_links(files, existing_pages)
        repository.validate_changes(linked, base_commit=base_commit)
        return linked

    def detect_conflicts(files: dict[str, str | None], proposed_conflicts: list[str]) -> list[str]:
        pending_pages = [f"pending_page:{path}" for path in files if path.startswith("pending/")]
        return sorted(set([*proposed_conflicts, *pending_pages]))

    def verify(
        revision_id: str,
        files: dict[str, str | None],
        conflicts: list[str],
        base_commit: str,
    ) -> VerificationOutcome:
        validate_citation_targets(files, db, wiki_id=run.wiki_id)
        trust: list[int] = []
        members = revision_ids if run.input_json.get("batch") else [revision_id]
        for member in members:
            revision = db.get(SourceRevision, member)
            if revision is None:
                raise ValueError("source revision does not exist")
            source = db.get(Source, revision.source_id)
            if source is None or source.wiki_id != run.wiki_id:
                raise ValueError("source does not exist in the current Wiki")
            trust.append(source.trust_level)
        factory = ModelFactory(db, settings, run.created_by_id)
        verifier_profile_id = factory.assigned_profile_id("planning") or profile_id
        model = factory.chat(
            verifier_profile_id, timeout_seconds=settings.compilation_model_timeout_seconds
        )
        minimum_source_trust = min(trust)
        db.commit()
        verification = WikiVerifier(model).verify(
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
            minimum_source_trust=minimum_source_trust,
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
        outcome = VerificationOutcome(
            issues=verification.issues,
            risk_violations=evaluate_change_risk(risk, policy),
        )
        db.commit()
        return outcome

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
    connection_string = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with PostgresSaver.from_conn_string(connection_string) as checkpointer:
        # Concurrent first-use migrations must not race on checkpoint tables and indexes.
        with checkpointer.conn.cursor() as cursor:
            cursor.execute("SET lock_timeout = '30s'")
            cursor.execute("SELECT pg_advisory_lock(hashtext('wiki-agent-checkpoint-migrations'))")
        try:
            checkpointer.setup()
        finally:
            with checkpointer.conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_unlock(hashtext('wiki-agent-checkpoint-migrations'))"
                )
                cursor.execute("RESET lock_timeout")
        yield checkpointer


def _change_ratio(
    repository: WikiRepository,
    path: str,
    content: str | None,
    existing_paths: set[str],
) -> float:
    if content is None or path not in existing_paths:
        return 0.0
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
    source_kind = source.kind
    db.commit()
    if source_kind is SourceKind.WEBSITE:
        source_url = revision.metadata_json.get("final_url")
        parsed = DocumentParser().parse(
            object_path, source_kind,
            source_url=source_url if isinstance(source_url, str) else None,
            media_type=revision.media_type,
        )
    else:
        parsed = DocumentParser().parse(object_path, source_kind)
    factory = ModelFactory(db, settings, run.created_by_id)
    embedding_profile_id = factory.assigned_profile_id("embedding")
    embeddings = factory.embeddings(embedding_profile_id) if embedding_profile_id else None
    db.commit()
    section_vectors = (
        embeddings.embed_documents([section.text for section in parsed.sections])
        if embeddings is not None
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
                embedding_profile_id=embedding_profile_id,
            )
        )
        marked_sections.append(f'<SOURCE_SPAN id="{span_id}">\n{section.text}\n</SOURCE_SPAN>')
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
