import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from openai import OpenAIError
from sqlalchemy import func, select

from wiki_agent.api.dependencies import (
    AppSettings,
    CurrentUser,
    CurrentWiki,
    DbSession,
    WikiRepo,
    require_feature,
)
from wiki_agent.api.schemas import (
    DraftCreate,
    DraftView,
    PublishRequest,
    RejectDraftRequest,
    RollbackRequest,
    SchemaUpdate,
    WikiSearchRequest,
)
from wiki_agent.api.sources import (
    cleanup_source_objects,
    delete_processing_runs,
    delete_source_records,
    prepare_source_deletion,
)
from wiki_agent.config import get_settings
from wiki_agent.llm import (
    ModelFactory,
    QueryPlan,
    QueryRewriteError,
    RerankerError,
    WikiQueryRewriter,
)
from wiki_agent.models import (
    AgentRun,
    AuditLog,
    Draft,
    DraftStatus,
    FeatureFlag,
    PersonalWiki,
    QueryLog,
    Source,
    SourceRevision,
    SourceSpan,
    WikiDocument,
    WikiIndexState,
)
from wiki_agent.search.fusion import FusedHit, merge_fused_hits
from wiki_agent.search.fusion import should_rerank as _should_rerank
from wiki_agent.search.repository import (
    RawSearchRepository,
    WikiSearchRepository,
    hybrid_search,
    raw_hybrid_search,
)
from wiki_agent.wiki import WikiPage
from wiki_agent.wiki.indexer import WikiIndexer
from wiki_agent.wiki.repository import StaleWikiRevisionError, WikiRepositoryError
from wiki_agent.wiki.validation import CitationResolutionError, validate_citation_targets

router = APIRouter(tags=["wiki"])
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchExecution:
    hits: list[FusedHit]
    layer: str
    fallback_used: bool
    channels: list[str]
    degradations: list[dict[str, str]]
    query_plan: QueryPlan
    wiki_quality: dict[str, object]
    reranker_used: bool


@router.get("/wiki/pages")
def list_pages(user: CurrentUser, repo: WikiRepo) -> dict[str, object]:
    return {"revision": repo.head(), "pages": repo.list_pages()}


@router.get("/wiki/schema")
def read_schema(user: CurrentUser, repo: WikiRepo) -> dict[str, str]:
    return {"revision": repo.head(), "content": repo.read_schema()}


@router.put("/wiki/schema")
def update_schema(
    payload: SchemaUpdate,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    repo: WikiRepo,
) -> dict[str, str]:
    try:
        result = repo.publish_schema(
            payload.content,
            base_commit=payload.base_commit,
            author_name=user.display_name,
            author_email=user.local_commit_email,
            message="更新知识编译规则",
        )
    except StaleWikiRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WikiRepositoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(
        AuditLog(
            actor_id=user.id,
            action="schema.updated",
            resource_type="knowledge_schema",
            resource_id="AGENTS.md",
            details={"commit": result.commit},
        )
    )
    index_state = db.get(WikiIndexState, (wiki.id, "published"))
    if index_state is not None:
        index_state.commit_sha = result.commit
        index_state.indexed_at = datetime.now(UTC)
    db.commit()
    return {"revision": result.commit, "content": payload.content}


@router.get("/wiki/graph")
def wiki_graph(user: CurrentUser, repo: WikiRepo) -> dict[str, object]:
    revision = repo.head()
    pages = {
        path: repo.read_page(path, revision)
        for path in repo.list_pages()
    }
    edges: dict[tuple[str, str], dict[str, str]] = {}
    for path, page in pages.items():
        targets = {
            target if target.endswith(".md") else f"{target}.md"
            for target in page.links
        }
        for target in (*page.metadata.sources, *sorted(targets)):
            if target not in pages or target == path:
                continue
            pair = tuple(sorted((path, target)))
            kind = "source" if target in page.metadata.sources else "related"
            if pair not in edges or kind == "source":
                edges[pair] = {"source": path, "target": target, "kind": kind}
    return {
        "revision": revision,
        "nodes": [
            {
                "path": path,
                "title": page.metadata.title,
                "kind": page.metadata.type.value,
            }
            for path, page in pages.items()
        ],
        "edges": list(edges.values()),
    }


@router.get("/wiki/pages/{path:path}")
def read_page(path: str, user: CurrentUser, repo: WikiRepo) -> dict[str, object]:
    try:
        page = repo.read_page(path)
    except (ValueError, WikiRepositoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return page.model_dump(mode="json")


@router.get("/wiki/history/{path:path}")
def page_history(path: str, user: CurrentUser, repo: WikiRepo) -> list[dict[str, str]]:
    try:
        return repo.history(path)
    except (ValueError, WikiRepositoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/conflicts")
def list_conflicts(
    user: CurrentUser, repo: WikiRepo, wiki: CurrentWiki, db: DbSession
) -> list[dict[str, object]]:
    batches: dict[str, tuple[str, str]] = {}
    published = list(db.scalars(
        select(Draft).where(
            Draft.wiki_id == wiki.id,
            Draft.created_by_id == user.id,
            Draft.status == DraftStatus.PUBLISHED,
        ).order_by(Draft.updated_at, Draft.id)
    ))
    published_ids = {draft.id for draft in published}
    batch_drafts = {
        draft_id: run.id
        for run in db.scalars(
            select(AgentRun).where(
                AgentRun.wiki_id == wiki.id, AgentRun.created_by_id == user.id,
                AgentRun.kind == "compile_source",
            )
        )
        if run.input_json.get("batch")
        and isinstance((draft_id := (run.result_json or {}).get("draft_id")), str)
        and draft_id in published_ids
    }
    for draft in published:
        for path in draft.files:
            if path.startswith("pending/"):
                if draft.id in batch_drafts:
                    batches[path] = (batch_drafts[draft.id], draft.title)
                else:
                    batches.pop(path, None)
    conflicts: list[dict[str, object]] = []
    for path in repo.list_pages():
        if not path.startswith("pending/"):
            continue
        page = repo.read_page(path)
        conflicts.append(
            {
                "path": path,
                "title": page.metadata.title,
                "status": page.metadata.status,
                "updated": page.metadata.updated,
                "sources": page.metadata.sources,
                "batch_id": batches[path][0] if path in batches else None,
                "batch_title": batches[path][1] if path in batches else None,
            }
        )
    return conflicts


@router.post(
    "/wiki/search",
    dependencies=[Depends(require_feature("wiki_questions"))],
)
def search_wiki(
    payload: WikiSearchRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> dict[str, object]:
    result = _execute_search(payload, db, user.id, wiki.id)
    _record_query(db, user.id, wiki.id, payload.query, result)
    return {
        "query": payload.query,
        "layer": result.layer,
        "fallback_used": result.fallback_used,
        "channels": result.channels,
        "degradations": result.degradations,
        "query_rewrite": result.query_plan.model_dump(mode="json"),
        "wiki_quality": result.wiki_quality,
        "reranker_used": result.reranker_used,
        "hits": [
            {
                "document_id": hit.document_id,
                "score": hit.score,
                "channels": hit.channels,
                "content": hit.content,
                "metadata": hit.metadata,
            }
            for hit in result.hits
        ],
    }


def _execute_search(
    payload: WikiSearchRequest,
    db: DbSession,
    user_id: str,
    wiki_id: str,
) -> SearchExecution:
    factory = ModelFactory(db, get_settings(), user_id)
    degradations: list[dict[str, str]] = []
    query_plan = QueryPlan(
        lexical_query=payload.query,
        semantic_query=payload.query,
    )
    answer_profile_id = factory.assigned_profile_id("answer")
    if answer_profile_id is not None:
        try:
            query_plan = WikiQueryRewriter(factory.chat(answer_profile_id)).rewrite(payload.query)
        except (
            QueryRewriteError,
            OpenAIError,
            httpx.HTTPError,
            NotImplementedError,
        ) as exc:
            logger.warning(
                "Query rewrite failed; using the original question",
                exc_info=exc,
                extra={"user_id": user_id, "wiki_id": wiki_id},
            )
            degradations.append(
                {
                    "channel": "query_rewrite",
                    "reason": "Query rewrite unavailable; the original question was used.",
                }
            )

    embedding = None
    embedding_profile_id = payload.embedding_profile_id or factory.assigned_profile_id("embedding")
    if embedding_profile_id:
        try:
            embedding = factory.embeddings(embedding_profile_id).embed_query(
                query_plan.semantic_query
            )
        except (OpenAIError, httpx.HTTPError) as exc:
            logger.warning(
                "Embedding retrieval failed; continuing without vector search",
                exc_info=exc,
                extra={"user_id": user_id, "wiki_id": wiki_id},
            )
            degradations.append(
                {
                    "channel": "vector",
                    "reason": "Embedding service unavailable; vector retrieval was skipped.",
                }
            )
    index_state = db.get(WikiIndexState, (wiki_id, "published"))
    wiki_embedding = (
        embedding
        if index_state is not None and index_state.embedding_profile_id == embedding_profile_id
        else None
    )
    candidate_limit = min(payload.limit * 3, 50)
    wiki_hits = hybrid_search(
        WikiSearchRepository(db, wiki_id),
        query_plan.lexical_query,
        embedding=wiki_embedding,
        entry_node_ids=payload.entry_node_ids,
        limit=candidate_limit,
    )
    wiki_quality = _evaluate_wiki_quality(
        wiki_hits,
        minimum_hits=payload.minimum_core_hits,
        require_cross_channel_top=payload.require_cross_channel_top,
    )
    raw_flag = db.get(FeatureFlag, "raw_rag")
    raw_allowed = (
        payload.allow_raw_fallback
        and get_settings().raw_rag_enabled
        and raw_flag is not None
        and raw_flag.enabled
    )
    fallback_used = raw_allowed and not bool(wiki_quality["sufficient"])
    raw_hits: list[FusedHit] = []
    if fallback_used:
        raw_hits = raw_hybrid_search(
            RawSearchRepository(db, wiki_id),
            query_plan.lexical_query,
            embedding=embedding,
            embedding_profile_id=embedding_profile_id,
            limit=candidate_limit,
        )
    hits = merge_fused_hits(
        [
            ("wiki", wiki_hits),
            ("raw", raw_hits),
        ],
        limit=candidate_limit,
    )
    reranker_profile_id = payload.reranker_profile_id or factory.assigned_profile_id("reranker")
    reranker_used = False
    if reranker_profile_id and _should_rerank(hits, fallback_used=fallback_used):
        try:
            hits = factory.reranker(reranker_profile_id).rerank(payload.query, hits, payload.limit)
            reranker_used = True
        except RerankerError as exc:
            logger.warning(
                "Reranker failed; preserving reciprocal-rank-fusion order",
                exc_info=exc,
                extra={"user_id": user_id, "wiki_id": wiki_id},
            )
            degradations.append(
                {
                    "channel": "reranker",
                    "reason": "Reranker unavailable; the fused retrieval order was preserved.",
                }
            )
    hits = hits[: payload.limit]
    active_channels = ["bm25", "graph"]
    if wiki_embedding is not None:
        active_channels.append("vector")
    if fallback_used:
        active_channels.append("raw_bm25")
        if embedding is not None:
            active_channels.append("raw_vector")
    layers = {str(hit.metadata.get("layer", "wiki")) for hit in hits}
    if "hybrid" in layers or ("wiki" in layers and "raw" in layers):
        layer = "hybrid"
    elif "raw" in layers or (not hits and fallback_used):
        layer = "raw"
    else:
        layer = "wiki"
    return SearchExecution(
        hits=hits,
        layer=layer,
        fallback_used=fallback_used,
        channels=active_channels,
        degradations=degradations,
        query_plan=query_plan,
        wiki_quality=wiki_quality,
        reranker_used=reranker_used,
    )


def _evaluate_wiki_quality(
    hits: list[FusedHit],
    *,
    minimum_hits: int,
    require_cross_channel_top: bool,
) -> dict[str, object]:
    top_channels = list(hits[0].channels) if hits else []
    if len(hits) < minimum_hits:
        return {
            "sufficient": False,
            "hit_count": len(hits),
            "top_channels": top_channels,
            "reason": f"Wiki returned fewer than {minimum_hits} results.",
        }
    if require_cross_channel_top and len(top_channels) < 2:
        return {
            "sufficient": False,
            "hit_count": len(hits),
            "top_channels": top_channels,
            "reason": "The top Wiki result was supported by only one retrieval channel.",
        }
    return {
        "sufficient": True,
        "hit_count": len(hits),
        "top_channels": top_channels,
        "reason": None,
    }


@router.get("/wiki/index/status")
def wiki_index_status(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> dict[str, object]:
    return _wiki_index_status(db, user.id, wiki.id)


@router.post("/wiki/index/rebuild")
def rebuild_current_wiki_index(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    repo: WikiRepo,
) -> dict[str, object]:
    factory = ModelFactory(db, get_settings(), user.id)
    embedding_profile_id = factory.assigned_profile_id("embedding")
    embeddings = (
        factory.embeddings(embedding_profile_id) if embedding_profile_id is not None else None
    )
    db.commit()
    try:
        commit = WikiIndexer(repo, wiki.id).rebuild(
            db,
            embeddings,
            embedding_profile_id=embedding_profile_id,
        )
    except (OpenAIError, httpx.HTTPError) as exc:
        db.rollback()
        logger.exception(
            "Wiki index rebuild failed",
            extra={"user_id": user.id, "wiki_id": wiki.id},
        )
        raise HTTPException(
            status_code=503,
            detail="Embedding service unavailable; Wiki index was not rebuilt.",
        ) from exc
    db.add(
        AuditLog(
            actor_id=user.id,
            action="wiki.index_rebuilt",
            resource_type="wiki_commit",
            resource_id=commit,
            details={"embedding_profile_id": embedding_profile_id},
        )
    )
    db.commit()
    return {"commit": commit, **_wiki_index_status(db, user.id, wiki.id)}


def _wiki_index_status(db: DbSession, user_id: str, wiki_id: str) -> dict[str, object]:
    embedding_profile_id = ModelFactory(db, get_settings(), user_id).assigned_profile_id(
        "embedding"
    )
    state = db.get(WikiIndexState, (wiki_id, "published"))
    wiki_document_count = (
        db.scalar(select(func.count(WikiDocument.id)).where(WikiDocument.wiki_id == wiki_id)) or 0
    )
    wiki_vector_count = (
        db.scalar(
            select(func.count(WikiDocument.id)).where(
                WikiDocument.wiki_id == wiki_id,
                WikiDocument.embedding.is_not(None),
            )
        )
        or 0
    )
    raw_span_query = (
        select(func.count(SourceSpan.id))
        .join(SourceRevision, SourceRevision.id == SourceSpan.source_revision_id)
        .join(Source, Source.id == SourceRevision.source_id)
        .where(Source.wiki_id == wiki_id)
    )
    raw_span_count = db.scalar(raw_span_query) or 0
    raw_vector_count = 0
    if embedding_profile_id is not None:
        raw_vector_count = (
            db.scalar(
                raw_span_query.where(
                    SourceSpan.embedding.is_not(None),
                    SourceSpan.embedding_profile_id == embedding_profile_id,
                )
            )
            or 0
        )
    indexed_profile_id = state.embedding_profile_id if state is not None else None
    return {
        "assigned_embedding_profile_id": embedding_profile_id,
        "indexed_embedding_profile_id": indexed_profile_id,
        "indexed_at": state.indexed_at if state is not None else None,
        "wiki_document_count": wiki_document_count,
        "wiki_vector_count": wiki_vector_count,
        "raw_span_count": raw_span_count,
        "raw_vector_count": raw_vector_count,
        "wiki_rebuild_required": bool(
            embedding_profile_id is not None
            and wiki_document_count > 0
            and (
                indexed_profile_id != embedding_profile_id
                or wiki_vector_count < wiki_document_count
            )
        ),
        "raw_backfill_required": bool(
            embedding_profile_id is not None and raw_vector_count < raw_span_count
        ),
    }


def _record_query(
    db: DbSession,
    user_id: str,
    wiki_id: str,
    query: str,
    result: SearchExecution,
) -> None:
    db.add(
        QueryLog(
            wiki_id=wiki_id,
            user_id=user_id,
            query=query,
            layer=result.layer,
            fallback_used=result.fallback_used,
            hit_count=len(result.hits),
            channels=result.channels,
        )
    )
    db.commit()


@router.post(
    "/drafts",
    response_model=DraftView,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_feature("wiki_editing"))],
)
def create_draft(
    payload: DraftCreate,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    repo: WikiRepo,
) -> Draft:
    if payload.base_commit != repo.head():
        raise HTTPException(status_code=409, detail="Draft base commit is stale")
    for path, content in payload.files.items():
        if content is not None:
            try:
                WikiPage.from_markdown(content)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"{path}: {exc}") from exc
    try:
        validate_citation_targets(payload.files, db, wiki_id=wiki.id)
    except CitationResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    draft = Draft(
        wiki_id=wiki.id,
        created_by_id=user.id,
        base_commit=payload.base_commit,
        files=payload.files,
        title=payload.title,
    )
    db.add(draft)
    db.flush()
    db.add(
        AuditLog(
            actor_id=user.id,
            action="draft.created",
            resource_type="draft",
            resource_id=draft.id,
            details={"title": draft.title},
        )
    )
    db.commit()
    db.refresh(draft)
    return draft


@router.get("/drafts", response_model=list[DraftView])
def list_my_drafts(user: CurrentUser, wiki: CurrentWiki, db: DbSession) -> list[Draft]:
    return list(
        db.scalars(
            select(Draft)
            .where(Draft.created_by_id == user.id, Draft.wiki_id == wiki.id)
            .order_by(Draft.created_at.desc())
        )
    )


@router.post(
    "/wiki/pages/{path:path}/rollback",
    response_model=DraftView,
    dependencies=[Depends(require_feature("wiki_rollback"))],
)
def create_rollback_draft(
    path: str,
    payload: RollbackRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    repo: WikiRepo,
) -> Draft:
    try:
        historical_page = repo.read_page(path, payload.target_commit)
    except (ValueError, WikiRepositoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    draft = Draft(
        wiki_id=wiki.id,
        created_by_id=user.id,
        base_commit=repo.head(),
        files={path: historical_page.to_markdown()},
        title=payload.title,
    )
    db.add(draft)
    db.flush()
    db.add(
        AuditLog(
            actor_id=user.id,
            action="draft.rollback_created",
            resource_type="draft",
            resource_id=draft.id,
            details={"path": path, "target_commit": payload.target_commit},
        )
    )
    db.commit()
    db.refresh(draft)
    return draft


@router.post("/drafts/{draft_id}/publish", response_model=DraftView)
def publish_draft(
    draft_id: str,
    payload: PublishRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    repo: WikiRepo,
) -> Draft:
    db.scalar(select(PersonalWiki.id).where(PersonalWiki.id == wiki.id).with_for_update())
    draft = db.scalar(select(Draft).where(Draft.id == draft_id).with_for_update())
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the draft creator can publish it")
    if draft.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.status is not DraftStatus.OPEN:
        raise HTTPException(status_code=409, detail="Draft is not open")
    try:
        validate_citation_targets(draft.files, db, wiki_id=wiki.id)
    except CitationResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = repo.publish(
            draft.files,
            base_commit=draft.base_commit,
            author_name=user.display_name,
            author_email=user.local_commit_email,
            message=payload.message.strip() or f"发布草稿：{draft.title}",
        )
    except StaleWikiRevisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WikiRepositoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    draft.status = DraftStatus.PUBLISHED
    draft.published_commit = result.commit
    db.add(
        AuditLog(
            actor_id=user.id,
            action="draft.published",
            resource_type="draft",
            resource_id=draft.id,
            details={"commit": result.commit, "paths": list(result.changed_paths)},
        )
    )
    db.commit()
    factory = ModelFactory(db, get_settings(), user.id)
    embedding_profile_id = factory.assigned_profile_id("embedding")
    embeddings = (
        factory.embeddings(embedding_profile_id) if embedding_profile_id is not None else None
    )
    db.commit()
    try:
        WikiIndexer(repo, wiki.id).rebuild(
            db,
            embeddings,
            embedding_profile_id=embedding_profile_id,
        )
    except Exception as exc:
        db.rollback()
        db.add(
            AuditLog(
                actor_id=user.id,
                action="wiki.index_failed",
                resource_type="wiki_commit",
                resource_id=result.commit,
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
        )
        db.commit()
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Wiki was published, but its search index could not be rebuilt",
                "commit": result.commit,
                "error": f"{type(exc).__name__}: {exc}",
            },
        ) from exc
    db.refresh(draft)
    return draft


@router.post("/drafts/{draft_id}/reject", status_code=status.HTTP_204_NO_CONTENT)
def reject_draft(
    draft_id: str,
    payload: RejectDraftRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
    settings: AppSettings,
) -> None:
    db.scalar(select(PersonalWiki.id).where(PersonalWiki.id == wiki.id).with_for_update())
    draft = db.scalar(select(Draft).where(Draft.id == draft_id).with_for_update())
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the draft creator can reject it")
    if draft.wiki_id != wiki.id:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.status is not DraftStatus.OPEN:
        raise HTTPException(status_code=409, detail="Draft is not open")
    linked_runs = list(db.scalars(select(AgentRun).where(
        AgentRun.wiki_id == wiki.id,
        AgentRun.created_by_id == user.id,
        AgentRun.result_json["draft_id"].as_string() == draft.id,
    )))
    source_ids: set[str] = set()
    for run in linked_runs:
        if run.kind != "compile_source":
            continue
        revisions = run.source_revision_ids
        if not revisions:
            raise HTTPException(status_code=409, detail="草稿的原件关联记录不完整，无法安全删除。")
        for revision_id in revisions:
            revision = db.get(SourceRevision, revision_id)
            source = db.get(Source, revision.source_id) if revision is not None else None
            if source is None or source.wiki_id != wiki.id:
                raise HTTPException(
                    status_code=409,
                    detail="草稿的原件关联记录不完整，无法安全删除。请刷新后重试。",
                )
            source_ids.add(source.id)
    deletions = [
        prepare_source_deletion(
            source_id, wiki.id, db, settings, ignored_draft_ids=frozenset({draft.id})
        )
        for source_id in sorted(source_ids)
    ]
    db.delete(draft)
    for deletion in deletions:
        delete_source_records(deletion, user.id, db)
    removed_run_ids = {run_id for deletion in deletions for run_id in deletion.run_ids}
    delete_processing_runs(db, tuple(
        run.id for run in linked_runs if run.id not in removed_run_ids
    ))
    db.add(
        AuditLog(
            actor_id=user.id,
            action="draft.rejected",
            resource_type="draft",
            resource_id=draft.id,
            details={
                "reason": payload.reason,
                "title": draft.title,
                "deleted_sources": len(deletions),
            },
        )
    )
    db.commit()
    cleanup_failed = False
    for deletion in deletions:
        try:
            cleanup_source_objects(deletion, db)
        except HTTPException as exc:
            if exc.detail != "Source deleted, but object cleanup failed":
                raise
            cleanup_failed = True
    if cleanup_failed:
        raise HTTPException(status_code=500, detail="Source deleted, but object cleanup failed")
