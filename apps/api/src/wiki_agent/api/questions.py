import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from wiki_agent.api.dependencies import CurrentUser, CurrentWiki, DbSession, require_feature
from wiki_agent.api.schemas import WikiQuestionRequest
from wiki_agent.config import get_settings
from wiki_agent.llm import ModelFactory, WikiAnswerer
from wiki_agent.llm.factory import ModelConfigurationError
from wiki_agent.models import (
    AuditLog,
    Conversation,
    ConversationMessage,
    QueryLog,
    QuestionRun,
    new_id,
)
from wiki_agent.qa.agent import (
    AgentCheckpoint,
    AgentOutcome,
    ConversationQuestionPlan,
    KnowledgeQuestionAgent,
    ToolCallingUnsupportedError,
)
from wiki_agent.qa.memory import prepare_history
from wiki_agent.qa.tools import KnowledgeTools
from wiki_agent.search.fusion import FusedHit

router = APIRouter(prefix="/questions", tags=["questions"])
logger = logging.getLogger(__name__)
NEW_CONVERSATION_TITLE = "新对话"
RUN_STALE_AFTER = timedelta(minutes=5)


class QuestionEvidenceView(BaseModel):
    document_id: str
    channels: list[str]
    metadata: dict[str, object]


class QuestionResponseView(BaseModel):
    answer: str
    citations: list[str]
    uncertainty: str | None
    insufficient_evidence: bool
    layer: Literal["wiki", "raw", "hybrid"]
    fallback_used: bool
    channels: list[str]
    degradations: list[dict[str, str]]
    query_rewrite: dict[str, str]
    wiki_quality: dict[str, object]
    reranker_used: bool
    evidence: list[QuestionEvidenceView]
    conversation_id: str
    conversation_title: str
    run_id: str
    message_id: str
    clarification_question: str | None


class ConversationSummaryView(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationRename(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if any(
            ord(character) < 32 or ord(character) == 127 or character in "\u2028\u2029"
            for character in value
        ):
            raise ValueError("Conversation title must not contain control characters")
        return value


class ConversationMessageView(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    citations: list[str]
    answer: QuestionResponseView | None
    created_at: datetime


class ConversationView(BaseModel):
    id: str
    title: str
    messages: list[ConversationMessageView]


class QuestionRunView(BaseModel):
    id: str
    status: str
    stage: str
    error: str | None
    conversation_id: str
    conversation_title: str
    question: str
    request_id: str
    resumable: bool
    answer: QuestionResponseView | None
    tool_trace: list[dict[str, object]]


class RunSuperseded(RuntimeError):
    pass


class RunCancelled(RuntimeError):
    pass


class HistoryCheckpoint(BaseModel):
    messages: list[tuple[str, str]]
    topic_memory: str
    active_entities: list[str]


class QuestionCheckpoint(BaseModel):
    title_generated: bool = False
    history: HistoryCheckpoint | None = None
    plan: ConversationQuestionPlan | None = None
    agent: AgentCheckpoint | None = None
    degradations: list[dict[str, str]] = Field(default_factory=list)
    summary_cache: dict[str, str] = Field(default_factory=dict)


def _check_lease(db: DbSession, run: QuestionRun, token: str) -> None:
    db.scalar(
        select(Conversation.id)
        .where(Conversation.id == run.conversation_id)
        .with_for_update()
    )
    claim = db.execute(
        update(QuestionRun)
        .where(
            QuestionRun.id == run.id,
            QuestionRun.lease_token == token,
            QuestionRun.status == "running",
        )
        .values(updated_at=datetime.now(UTC))
        .returning(QuestionRun.id)
        .execution_options(synchronize_session=False)
    )
    if claim.scalar_one_or_none() is None:
        status = db.scalar(select(QuestionRun.status).where(QuestionRun.id == run.id))
        if status == "cancelled":
            raise RunCancelled("Question execution was cancelled")
        raise RunSuperseded("Question execution was replaced by a newer attempt")
    db.refresh(run)


class StoredEvidence(BaseModel):
    document_id: str
    score: float
    channels: list[str]
    content: str
    metadata: dict[str, object] = Field(default_factory=dict)


def _owned_conversation(
    db: DbSession, conversation_id: str, user_id: str, wiki_id: str
) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.wiki_id == wiki_id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def _owned_run(db: DbSession, run_id: str, user_id: str, wiki_id: str) -> QuestionRun:
    run = db.scalar(
        select(QuestionRun).where(
            QuestionRun.id == run_id,
            QuestionRun.user_id == user_id,
            QuestionRun.wiki_id == wiki_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Question run not found")
    return run


def _stored_evidence(run: QuestionRun) -> list[FusedHit]:
    return [
        FusedHit(
            document_id=record.document_id,
            score=record.score,
            channels=tuple(record.channels),
            content=record.content,
            metadata=record.metadata,
        )
        for record in (StoredEvidence.model_validate(item) for item in run.evidence_json)
    ]


def _save_progress(
    db: DbSession,
    run: QuestionRun,
    token: str,
    stage: str,
    trace: list[dict[str, object]],
    evidence: list[FusedHit],
) -> None:
    _check_lease(db, run, token)
    run.stage = stage
    run.tool_trace = list(trace)
    run.evidence_json = [
        {
            "document_id": hit.document_id,
            "score": hit.score,
            "channels": list(hit.channels),
            "content": hit.content,
            "metadata": hit.metadata,
        }
        for hit in evidence
    ]
    db.commit()


def _save_checkpoint(
    db: DbSession, run: QuestionRun, token: str, checkpoint: QuestionCheckpoint
) -> None:
    _check_lease(db, run, token)
    run.checkpoint_json = checkpoint.model_dump(mode="json")
    if checkpoint.agent is not None:
        state = checkpoint.agent
        run.stage = (
            "answering" if state.phase in {"answering", "completed"}
            else "reranking" if state.phase == "reranking"
            else "retrieving"
        )
        run.tool_trace = list(state.trace)
        evidence = (
            state.final_evidence if state.phase in {"answering", "completed"} else state.evidence
        )
        run.evidence_json = [record.model_dump(mode="json") for record in evidence]
    elif checkpoint.plan is not None:
        run.stage = "retrieving"
    else:
        run.stage = "resolving"
    if checkpoint.plan is not None:
        run.standalone_question = checkpoint.plan.standalone_question
        run.active_entities = list(checkpoint.plan.active_entities)
    db.commit()


def _answer_payload(
    run: QuestionRun, outcome: AgentOutcome, *, conversation_title: str
) -> dict[str, object]:
    evidence = outcome.evidence
    layers = {hit.metadata.get("layer", "wiki") for hit in evidence}
    layer = "hybrid" if len(layers) > 1 else str(next(iter(layers), "wiki"))
    channels = sorted({channel for hit in evidence for channel in hit.channels})
    raw_searched = any(
        item.get("tool") in {"SearchRaw", "ReadSourceSpan"} for item in outcome.trace
    )
    return {
        **outcome.answer.model_dump(mode="json"),
        "layer": layer,
        "fallback_used": raw_searched or "raw" in layers,
        "channels": channels,
        "degradations": outcome.degradations,
        "query_rewrite": {
            "lexical_query": outcome.standalone_question,
            "semantic_query": outcome.standalone_question,
        },
        "wiki_quality": {
            "sufficient": bool(evidence) and not outcome.answer.insufficient_evidence,
            "hit_count": len(evidence),
            "top_channels": list(evidence[0].channels) if evidence else [],
            "reason": (
                None
                if evidence and not outcome.answer.insufficient_evidence
                else "The answer is not supported by enough evidence."
            ),
        },
        "reranker_used": outcome.reranker_used,
        "evidence": [
            {
                "document_id": hit.document_id,
                "channels": list(hit.channels),
                "metadata": hit.metadata,
            }
            for hit in evidence
            if hit.document_id in outcome.answer.citations
        ],
        "conversation_id": run.conversation_id,
        "conversation_title": conversation_title,
        "run_id": run.id,
        "message_id": "",
        "clarification_question": outcome.clarification_question,
    }


def _complete_run(
    db: DbSession, run: QuestionRun, outcome: AgentOutcome, token: str
) -> dict[str, object]:
    _check_lease(db, run, token)
    conversation = db.get(Conversation, run.conversation_id)
    if conversation is None:
        raise ValueError("Conversation was removed during question execution")
    db.refresh(conversation)
    response = _answer_payload(run, outcome, conversation_title=conversation.title)
    user_message = ConversationMessage(
        conversation_id=run.conversation_id,
        role="user",
        content=run.question,
    )
    db.add(user_message)
    db.flush()
    assistant_message = ConversationMessage(
        conversation_id=run.conversation_id,
        role="assistant",
        content=outcome.answer.answer,
        citations=list(outcome.answer.citations),
        answer_json=None,
    )
    db.add(assistant_message)
    db.flush()
    response["message_id"] = assistant_message.id
    assistant_message.answer_json = dict(response)
    run.status = "completed"
    run.stage = "completed"
    run.message_id = assistant_message.id
    run.answer_json = response
    run.standalone_question = outcome.standalone_question
    run.tool_trace = list(outcome.trace)
    if outcome.clarification_question is None:
        db.add(
            QueryLog(
                wiki_id=run.wiki_id,
                user_id=run.user_id,
                query=run.question,
                layer=str(response["layer"]),
                fallback_used=bool(response["fallback_used"]),
                hit_count=len(outcome.evidence),
                channels=sorted({channel for hit in outcome.evidence for channel in hit.channels}),
            )
        )
    if conversation is not None:
        conversation.updated_at = datetime.now(UTC)
        if outcome.clarification_question is None and outcome.active_entities:
            conversation.active_entities = outcome.active_entities
    db.commit()
    return response


def _execute_run(
    db: DbSession,
    run: QuestionRun,
    *,
    profile_id: str,
    resume_evidence: bool = False,
) -> dict[str, object]:
    token = run.lease_token
    run_id, wiki_id, user_id = run.id, run.wiki_id, run.user_id
    try:
        options = WikiQuestionRequest.model_validate({"query": run.question, **run.options_json})
        model = ModelFactory(db, get_settings(), run.user_id).chat(profile_id)
        checkpoint = QuestionCheckpoint.model_validate(run.checkpoint_json)
        degraded = checkpoint.degradations
        tools = KnowledgeTools(
            db,
            get_settings(),
            run.user_id,
            run.wiki_id,
            on_degradation=degraded.append,
            embedding_profile_id=options.embedding_profile_id,
            reranker_profile_id=options.reranker_profile_id,
            allow_raw=options.allow_raw_fallback,
            limit=options.limit,
            entry_node_ids=options.entry_node_ids,
        )
        agent = KnowledgeQuestionAgent(model, tools)
        def before_step() -> None:
            _check_lease(db, run, token)
            db.commit()

        def save_agent(state: AgentCheckpoint) -> None:
            checkpoint.agent = state
            _save_checkpoint(db, run, token, checkpoint)

        conversation = db.get(Conversation, run.conversation_id)
        if conversation is None:
            raise ValueError("Conversation was removed during question execution")
        if not checkpoint.title_generated:
            if conversation.title == NEW_CONVERSATION_TITLE and not conversation.title_is_custom:
                before_step()
                title = agent.title(run.question)
                _check_lease(db, run, token)
                db.refresh(conversation)
                if not conversation.title_is_custom:
                    conversation.title = title
            checkpoint.title_generated = True
            _save_checkpoint(db, run, token, checkpoint)
        if resume_evidence and checkpoint.agent is None:
            before_step()
            evidence = _stored_evidence(run)
            layers = {hit.metadata.get("layer", "wiki") for hit in evidence}
            layer = "hybrid" if len(layers) > 1 else str(next(iter(layers), "wiki"))
            answer = WikiAnswerer(model).answer(run.question, evidence, layer=layer)
            outcome = AgentOutcome(
                answer=answer,
                evidence=evidence,
                standalone_question=run.standalone_question or run.question,
                trace=list(run.tool_trace),
                active_entities=list(run.active_entities),
            )
        else:
            if checkpoint.history is None:
                before_step()
                prepared = prepare_history(
                    db,
                    conversation,
                    model,
                    run.question,
                    byte_budget=get_settings().qa_history_context_byte_budget,
                    before_model=before_step,
                    summary_cache=checkpoint.summary_cache,
                    on_summary=lambda: _save_checkpoint(db, run, token, checkpoint),
                )
                checkpoint.history = HistoryCheckpoint(
                    messages=prepared.messages,
                    topic_memory=prepared.topic_memory,
                    active_entities=prepared.active_entities,
                )
                _save_checkpoint(db, run, token, checkpoint)
            history = checkpoint.history.messages
            if checkpoint.plan is None:
                before_step()
                checkpoint.plan = agent.resolve(
                    run.question,
                    history,
                    topic_memory=checkpoint.history.topic_memory,
                    active_entities=checkpoint.history.active_entities,
                )
                _save_checkpoint(db, run, token, checkpoint)
            outcome = agent.answer(
                run.question,
                checkpoint.plan,
                history,
                history_byte_budget=get_settings().qa_history_context_byte_budget,
                checkpoint=checkpoint.agent,
                on_checkpoint=save_agent,
                before_step=before_step,
            )
        if degraded:
            outcome = replace(outcome, degradations=[*outcome.degradations, *degraded])
        return _complete_run(db, run, outcome, token)
    except (RunSuperseded, RunCancelled) as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "Question run was cancelled" if isinstance(exc, RunCancelled)
                    else "Question run was resumed elsewhere"
                ),
                "run_id": run_id,
            },
        ) from exc
    except Exception as exc:
        db.rollback()
        unsupported = isinstance(exc, ToolCallingUnsupportedError)
        marked = db.execute(
            update(QuestionRun)
            .where(
                QuestionRun.id == run_id,
                QuestionRun.lease_token == token,
                QuestionRun.status == "running",
            )
            .values(
                status="failed",
                error=(
                    "The selected answer model does not support tool calling."
                    if unsupported
                    else f"{type(exc).__name__}: question processing failed"
                ),
            )
            .returning(QuestionRun.id)
            .execution_options(synchronize_session=False)
        )
        did_mark = marked.scalar_one_or_none() is not None
        db.commit()
        if not did_mark:
            status = db.scalar(select(QuestionRun.status).where(QuestionRun.id == run_id))
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "Question run was cancelled" if status == "cancelled"
                        else "Question run was removed" if status is None
                        else "Question run was resumed elsewhere"
                    ),
                    "run_id": run_id,
                },
            ) from exc
        logger.exception(
            "Question agent failed",
            extra={"run_id": run_id, "wiki_id": wiki_id, "user_id": user_id},
        )
        raise HTTPException(
            status_code=422 if unsupported else 503,
            detail={
                "message": (
                    "The selected answer model does not support tool calling."
                    if unsupported
                    else "Question processing failed"
                ),
                "run_id": run_id,
            },
        ) from exc


@router.post("/conversations", response_model=ConversationSummaryView, status_code=201)
def create_conversation(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> Conversation:
    conversation = Conversation(
        user_id=user.id, wiki_id=wiki.id, title=NEW_CONVERSATION_TITLE
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/conversations", response_model=list[ConversationSummaryView])
def list_conversations(
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> list[dict[str, object]]:
    items = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.wiki_id == wiki.id)
        .order_by(Conversation.updated_at.desc())
    )
    return [
        {
            "id": item.id,
            "title": item.title,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in items
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationView)
def get_conversation(
    conversation_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> dict[str, object]:
    conversation = _owned_conversation(db, conversation_id, user.id, wiki.id)
    messages = db.scalars(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation.id)
        .order_by(ConversationMessage.created_at, ConversationMessage.id)
    )
    return {
        "id": conversation.id,
        "title": conversation.title,
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "citations": message.citations,
                "answer": (
                    {**message.answer_json, "conversation_title": conversation.title}
                    if message.answer_json is not None else None
                ),
                "created_at": message.created_at,
            }
            for message in messages
        ],
    }


@router.patch("/conversations/{conversation_id}", response_model=ConversationSummaryView)
def rename_conversation(
    conversation_id: str,
    payload: ConversationRename,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> Conversation:
    conversation = db.scalar(
        select(Conversation)
        .where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
            Conversation.wiki_id == wiki.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    previous = conversation.title
    conversation.title = payload.title
    conversation.title_is_custom = True
    conversation.updated_at = datetime.now(UTC)
    db.add(
        AuditLog(
            actor_id=user.id,
            action="conversation.renamed",
            resource_type="conversation",
            resource_id=conversation.id,
            details={"title": payload.title, "previous_title": previous},
        )
    )
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/runs/{run_id}", response_model=QuestionRunView)
def get_question_run(
    run_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> dict[str, object]:
    run = _owned_run(db, run_id, user.id, wiki.id)
    return _run_view(db, run)


def _run_view(db: DbSession, run: QuestionRun) -> dict[str, object]:
    conversation = db.get(Conversation, run.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    updated_at = run.updated_at.replace(tzinfo=run.updated_at.tzinfo or UTC)
    return {
        "id": run.id,
        "status": run.status,
        "stage": run.stage,
        "error": run.error,
        "conversation_id": run.conversation_id,
        "conversation_title": conversation.title,
        "question": run.question,
        "request_id": run.request_id,
        "resumable": run.status == "failed" or (
            run.status == "running" and updated_at <= datetime.now(UTC) - RUN_STALE_AFTER
        ),
        "answer": (
            {**run.answer_json, "conversation_title": conversation.title}
            if run.answer_json is not None else None
        ),
        "tool_trace": run.tool_trace,
    }


@router.get("/requests/{request_id}", response_model=QuestionRunView | None)
def get_question_request(
    request_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> dict[str, object] | None:
    run = db.scalar(
        select(QuestionRun).where(
            QuestionRun.user_id == user.id,
            QuestionRun.wiki_id == wiki.id,
            QuestionRun.request_id == request_id,
        )
    )
    return _run_view(db, run) if run is not None else None


@router.post("/runs/{run_id}/cancel", response_model=QuestionRunView)
def cancel_question(
    run_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> dict[str, object]:
    run = _owned_run(db, run_id, user.id, wiki.id)
    db.execute(
        update(QuestionRun)
        .where(
            QuestionRun.id == run.id,
            QuestionRun.status.in_(["running", "failed"]),
        )
        .values(status="cancelled", stage="cancelled", error=None, lease_token=new_id())
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.refresh(run)
    return _run_view(db, run)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> None:
    conversation = _owned_conversation(db, conversation_id, user.id, wiki.id)
    active = db.scalar(
        select(func.count(QuestionRun.id)).where(
            QuestionRun.conversation_id == conversation.id,
            QuestionRun.status == "running",
            QuestionRun.updated_at > datetime.now(UTC) - RUN_STALE_AFTER,
        )
    )
    if active:
        raise HTTPException(status_code=409, detail="Cannot delete a running conversation")
    db.delete(conversation)
    db.commit()


@router.post(
    "/runs/{run_id}/resume",
    response_model=QuestionResponseView,
    dependencies=[Depends(require_feature("wiki_questions"))],
)
def resume_question(
    run_id: str,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> dict[str, object]:
    run = db.scalar(
        select(QuestionRun)
        .where(
            QuestionRun.id == run_id,
            QuestionRun.user_id == user.id,
            QuestionRun.wiki_id == wiki.id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Question run not found")
    db.scalar(
        select(Conversation.id)
        .where(Conversation.id == run.conversation_id)
        .with_for_update()
    )
    db.refresh(run, with_for_update=True)
    if run.status == "completed":
        if run.answer_json is None:
            raise HTTPException(status_code=409, detail="Completed question has no answer")
        return {
            **run.answer_json,
            "conversation_title": _owned_conversation(
                db, run.conversation_id, user.id, wiki.id
            ).title,
        }
    updated_at = run.updated_at.replace(tzinfo=run.updated_at.tzinfo or UTC)
    if run.status == "running" and updated_at > datetime.now(UTC) - RUN_STALE_AFTER:
        raise HTTPException(status_code=409, detail="Question is still running")
    if run.status not in {"failed", "running"}:
        raise HTTPException(status_code=409, detail="Question cannot be resumed")
    active = db.scalar(
        select(QuestionRun.id).where(
            QuestionRun.conversation_id == run.conversation_id,
            QuestionRun.id != run.id,
            QuestionRun.status == "running",
        )
    )
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "Another question is running", "run_id": active},
        )
    profile_id = run.model_profile_id
    resume_evidence = run.stage == "answering" and bool(run.evidence_json)
    run.status = "running"
    run.error = None
    run.lease_token = new_id()
    db.commit()
    return _execute_run(db, run, profile_id=profile_id, resume_evidence=resume_evidence)


@router.post(
    "",
    response_model=QuestionResponseView,
    dependencies=[Depends(require_feature("wiki_questions"))],
)
def ask_question(
    payload: WikiQuestionRequest,
    user: CurrentUser,
    wiki: CurrentWiki,
    db: DbSession,
) -> dict[str, object]:
    request_id = payload.request_id or new_id()
    option_payload = payload.model_dump(
        include={
            "chat_profile_id",
            "embedding_profile_id",
            "reranker_profile_id",
            "allow_raw_fallback",
            "entry_node_ids",
            "limit",
        }
    )
    existing_request = db.scalar(
        select(QuestionRun).where(
            QuestionRun.user_id == user.id,
            QuestionRun.wiki_id == wiki.id,
            QuestionRun.request_id == request_id,
        )
    )
    conversation: Conversation | None
    if payload.conversation_id:
        conversation = _owned_conversation(db, payload.conversation_id, user.id, wiki.id)
        if existing_request is not None and existing_request.conversation_id != conversation.id:
            raise HTTPException(
                status_code=409, detail="Request ID belongs to another conversation"
            )
    else:
        if existing_request is not None:
            conversation = _owned_conversation(
                db, existing_request.conversation_id, user.id, wiki.id
            )
        else:
            conversation = None
    prior = existing_request if conversation is not None else None
    if prior is not None:
        assert conversation is not None
        if prior.question != payload.query:
            raise HTTPException(status_code=409, detail="Request ID was used for another question")
        if prior.options_json != option_payload:
            raise HTTPException(
                status_code=409, detail="Request ID was used with different options"
            )
        if prior.status == "completed" and prior.answer_json is not None:
            return {
                **prior.answer_json,
                "conversation_title": conversation.title,
            }
        raise HTTPException(
            status_code=409,
            detail={"message": "Question run already exists", "run_id": prior.id},
        )
    if conversation is not None:
        db.scalar(
            select(Conversation.id).where(Conversation.id == conversation.id).with_for_update()
        )
    factory = ModelFactory(db, get_settings(), user.id)
    profile_id = payload.chat_profile_id or factory.assigned_profile_id("answer")
    if profile_id is None:
        raise HTTPException(status_code=422, detail="No answer model is assigned")
    try:
        factory.chat(profile_id)
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if conversation is None:
        conversation = Conversation(
            user_id=user.id, wiki_id=wiki.id, title=NEW_CONVERSATION_TITLE
        )
        db.add(conversation)
        db.flush()
    active = db.scalar(
        select(QuestionRun.id).where(
            QuestionRun.conversation_id == conversation.id,
            QuestionRun.status == "running",
        )
    )
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail={"message": "Another question is running", "run_id": active},
        )
    run = QuestionRun(
        conversation_id=conversation.id,
        wiki_id=wiki.id,
        user_id=user.id,
        request_id=request_id,
        model_profile_id=profile_id,
        question=payload.query,
        options_json=option_payload,
        status="running",
        stage="resolving",
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        duplicate = db.scalar(
            select(QuestionRun).where(
                QuestionRun.user_id == user.id,
                QuestionRun.wiki_id == wiki.id,
                QuestionRun.request_id == request_id,
            )
        )
        if duplicate is not None and duplicate.question == payload.query:
            if duplicate.status == "completed" and duplicate.answer_json is not None:
                return {
                    **duplicate.answer_json,
                    "conversation_title": _owned_conversation(
                        db, duplicate.conversation_id, user.id, wiki.id
                    ).title,
                }
            raise HTTPException(
                status_code=409,
                detail={"message": "Question run already exists", "run_id": duplicate.id},
            ) from exc
        raise
    return _execute_run(db, run, profile_id=profile_id)
