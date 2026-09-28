from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from wiki_agent.api.dependencies import CurrentUser, DbSession, WikiRepo, require_feature
from wiki_agent.api.schemas import (
    DraftCreate,
    DraftView,
    PublishRequest,
    RejectDraftRequest,
    RollbackRequest,
    WikiQuestionRequest,
    WikiSearchRequest,
)
from wiki_agent.config import get_settings
from wiki_agent.llm import AnswerResult, ModelFactory, WikiAnswerer
from wiki_agent.models import AuditLog, Draft, DraftStatus, FeatureFlag, QueryLog
from wiki_agent.search.fusion import FusedHit
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


@dataclass(frozen=True)
class SearchExecution:
    hits: list[FusedHit]
    layer: str
    fallback_used: bool
    channels: list[str]


@router.get("/wiki/pages")
def list_pages(user: CurrentUser, repo: WikiRepo) -> dict[str, object]:
    return {"revision": repo.head(), "pages": repo.list_pages()}


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
def list_conflicts(user: CurrentUser, repo: WikiRepo) -> list[dict[str, object]]:
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
    db: DbSession,
) -> dict[str, object]:
    result = _execute_search(payload, db)
    _record_query(db, user.id, payload.query, result)
    return {
        "query": payload.query,
        "layer": result.layer,
        "fallback_used": result.fallback_used,
        "channels": result.channels,
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


@router.post(
    "/questions",
    dependencies=[Depends(require_feature("wiki_questions"))],
)
def ask_wiki(
    payload: WikiQuestionRequest,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, object]:
    result = _execute_search(payload, db)
    _record_query(db, user.id, payload.query, result)
    factory = ModelFactory(db, get_settings())
    chat_profile_id = payload.chat_profile_id or factory.assigned_profile_id("answer")
    if not result.hits:
        answer = AnswerResult(
            answer="No supporting evidence was found in the Wiki or enabled raw sources.",
            uncertainty="The knowledge base does not currently cover this question.",
            insufficient_evidence=True,
        )
    else:
        if chat_profile_id is None:
            raise HTTPException(
                status_code=422,
                detail="No answer model is assigned and chat_profile_id was not provided",
            )
        answer = WikiAnswerer(factory.chat(chat_profile_id)).answer(
            payload.query, result.hits, layer=result.layer
        )
    return {
        **answer.model_dump(mode="json"),
        "layer": result.layer,
        "fallback_used": result.fallback_used,
        "channels": result.channels,
        "evidence": [
            {
                "document_id": hit.document_id,
                "channels": hit.channels,
                "metadata": hit.metadata,
            }
            for hit in result.hits
            if hit.document_id in answer.citations
        ],
    }


def _execute_search(payload: WikiSearchRequest, db: DbSession) -> SearchExecution:
    factory = ModelFactory(db, get_settings())
    embedding = None
    embedding_profile_id = (
        payload.embedding_profile_id or factory.assigned_profile_id("embedding")
    )
    if embedding_profile_id:
        embedding = factory.embeddings(embedding_profile_id).embed_query(payload.query)
    hits = hybrid_search(
        WikiSearchRepository(db),
        payload.query,
        embedding=embedding,
        entry_node_ids=payload.entry_node_ids,
        limit=payload.limit,
    )
    raw_flag = db.get(FeatureFlag, "raw_rag")
    raw_allowed = (
        payload.allow_raw_fallback
        and get_settings().raw_rag_enabled
        and raw_flag is not None
        and raw_flag.enabled
    )
    fallback_used = raw_allowed and len(hits) < payload.minimum_core_hits
    if fallback_used:
        hits = raw_hybrid_search(
            RawSearchRepository(db),
            payload.query,
            embedding=embedding,
            limit=payload.limit,
        )
    reranker_profile_id = (
        payload.reranker_profile_id or factory.assigned_profile_id("reranker")
    )
    if reranker_profile_id and hits:
        hits = factory.reranker(reranker_profile_id).rerank(
            payload.query, hits, payload.limit
        )
    if fallback_used:
        active_channels = ["raw_bm25", "raw_vector"] if embedding else ["raw_bm25"]
    else:
        active_channels = ["bm25", "graph", "vector"] if embedding else ["bm25", "graph"]
    return SearchExecution(
        hits=hits,
        layer="raw" if fallback_used else "wiki",
        fallback_used=fallback_used,
        channels=active_channels,
    )


def _record_query(
    db: DbSession,
    user_id: str,
    query: str,
    result: SearchExecution,
) -> None:
    db.add(
        QueryLog(
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
def create_draft(payload: DraftCreate, user: CurrentUser, db: DbSession, repo: WikiRepo) -> Draft:
    if payload.base_commit != repo.head():
        raise HTTPException(status_code=409, detail="Draft base commit is stale")
    for path, content in payload.files.items():
        if content is not None:
            try:
                WikiPage.from_markdown(content)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"{path}: {exc}") from exc
    try:
        validate_citation_targets(payload.files, db)
    except CitationResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    draft = Draft(
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
def list_my_drafts(user: CurrentUser, db: DbSession) -> list[Draft]:
    return list(
        db.scalars(
            select(Draft)
            .where(Draft.created_by_id == user.id)
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
    db: DbSession,
    repo: WikiRepo,
) -> Draft:
    try:
        historical_page = repo.read_page(path, payload.target_commit)
    except (ValueError, WikiRepositoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    draft = Draft(
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
    db: DbSession,
    repo: WikiRepo,
) -> Draft:
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the draft creator can publish it")
    if draft.status is not DraftStatus.OPEN:
        raise HTTPException(status_code=409, detail="Draft is not open")
    try:
        validate_citation_targets(draft.files, db)
    except CitationResolutionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = repo.publish(
            draft.files,
            base_commit=draft.base_commit,
            author_name=user.display_name,
            author_email=user.email,
            message=payload.message,
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
    factory = ModelFactory(db, get_settings())
    embedding_profile_id = factory.assigned_profile_id("embedding")
    embeddings = (
        factory.embeddings(embedding_profile_id) if embedding_profile_id is not None else None
    )
    try:
        WikiIndexer(repo).rebuild(db, embeddings)
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


@router.post("/drafts/{draft_id}/reject", response_model=DraftView)
def reject_draft(
    draft_id: str,
    payload: RejectDraftRequest,
    user: CurrentUser,
    db: DbSession,
) -> Draft:
    draft = db.get(Draft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Only the draft creator can reject it")
    if draft.status is not DraftStatus.OPEN:
        raise HTTPException(status_code=409, detail="Draft is not open")
    draft.status = DraftStatus.REJECTED
    db.add(
        AuditLog(
            actor_id=user.id,
            action="draft.rejected",
            resource_type="draft",
            resource_id=draft.id,
            details={"reason": payload.reason},
        )
    )
    db.commit()
    db.refresh(draft)
    return draft
