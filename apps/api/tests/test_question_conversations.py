import pytest
from fastapi import HTTPException
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from wiki_agent.api import questions
from wiki_agent.api.schemas import WikiQuestionRequest
from wiki_agent.db import Base
from wiki_agent.llm.answerer import AnswerResult
from wiki_agent.models import (
    Conversation,
    ConversationMessage,
    PersonalWiki,
    QuestionRun,
    User,
    UserRole,
    WikiDocument,
)
from wiki_agent.qa.agent import ConversationQuestionPlan, ConversationTitle
from wiki_agent.qa.memory import MemorySummary


class FakeStructured:
    def __init__(self, schema: type) -> None:
        self.schema = schema

    def invoke(self, messages: object) -> object:
        if self.schema is ConversationTitle:
            return {"title": "Safety procedures"}
        if self.schema is MemorySummary:
            return {"topic_memory": "用户正在询问安全检查清单。"}
        if self.schema is ConversationQuestionPlan:
            return {"standalone_question": "Safety procedures"}
        assert self.schema is AnswerResult
        return {"answer": "Use the safety checklist.", "citations": ["concepts/safety.md"]}


class FakeModel:
    def __init__(self) -> None:
        self.count = 0

    def with_structured_output(self, schema: type, *, method: str) -> FakeStructured:
        assert method == "function_calling"
        return FakeStructured(schema)

    def bind_tools(self, schemas: list[type]) -> "FakeModel":
        return self

    def invoke(self, messages: object) -> AIMessage:
        self.count += 1
        if self.count % 2 == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"call-{self.count}",
                        "name": "SearchWiki",
                        "args": {"query": "Safety"},
                    }
                ],
            )
        return AIMessage(content="Found it")


class FakeModelFactory:
    model = FakeModel()

    def __init__(self, *args, **kwargs) -> None:
        pass

    def assigned_profile_id(self, purpose: str) -> str | None:
        return "answer" if purpose == "answer" else None

    def chat(self, profile_id: str) -> FakeModel:
        assert profile_id == "answer"
        return self.model


def make_user(db: Session, email: str) -> User:
    user = User(email=email, display_name=email, password_hash="unused", role=UserRole.USER)
    db.add(user)
    db.flush()
    return user


def test_agent_question_rejects_deterministic_only_thresholds() -> None:
    with pytest.raises(ValidationError, match="minimum_core_hits"):
        WikiQuestionRequest(query="Safety?", minimum_core_hits=1)


def test_empty_conversations_are_listed_and_history_is_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = make_user(db, "owner@qq.com")
        other = make_user(db, "other@qq.com")
        wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
        second_wiki = PersonalWiki(owner_id=owner.id, name="Second Wiki")
        db.add_all([wiki, second_wiki])
        db.commit()

        first = questions.create_conversation(owner, wiki, db)
        second = questions.create_conversation(owner, wiki, db)
        foreign = questions.create_conversation(owner, second_wiki, db)
        assert questions.ConversationSummaryView.model_validate(
            first, from_attributes=True
        ).id == first.id
        assert first.id != second.id
        assert first.title == second.title == questions.NEW_CONVERSATION_TITLE
        assert {item["id"] for item in questions.list_conversations(owner, wiki, db)} == {
            first.id, second.id
        }
        assert questions.list_conversations(other, wiki, db) == []
        assert questions.get_conversation(first.id, owner, wiki, db)["messages"] == []
        assert questions.get_conversation(second.id, owner, wiki, db)["messages"] == []
        assert [item["id"] for item in questions.list_conversations(owner, second_wiki, db)] == [
            foreign.id
        ]
        with pytest.raises(HTTPException) as denied:
            questions.get_conversation(first.id, other, wiki, db)
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as denied_wiki:
            questions.get_conversation(first.id, owner, second_wiki, db)
        assert denied_wiki.value.status_code == 404

        response = questions.ask_question(
            WikiQuestionRequest(
                query="Safety?", conversation_id=first.id, request_id="first-conversation"
            ),
            owner, wiki, db,
        )
        assert response["conversation_id"] == first.id
        assert db.get(Conversation, first.id).title == "Safety procedures"
        assert response["conversation_title"] == "Safety procedures"
        assert db.get(Conversation, second.id).title == questions.NEW_CONVERSATION_TITLE
        assert [message["role"] for message in questions.get_conversation(
            first.id, owner, wiki, db
        )["messages"]] == ["user", "assistant"]
        assert questions.get_conversation(second.id, owner, wiki, db)["messages"] == []


def test_conversation_history_includes_older_sessions_and_turns() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = make_user(db, "owner@qq.com")
        wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
        db.add(wiki)
        db.flush()
        conversations = [
            Conversation(user_id=owner.id, wiki_id=wiki.id, title=f"Conversation {index}")
            for index in range(101)
        ]
        db.add_all(conversations)
        db.flush()
        db.add_all([
            ConversationMessage(
                conversation_id=conversations[0].id, role="user", content=f"Question {index}"
            )
            for index in range(201)
        ])
        db.commit()

        assert len(questions.list_conversations(owner, wiki, db)) == 101
        messages = questions.get_conversation(conversations[0].id, owner, wiki, db)[
            "messages"
        ]
        assert len(messages) == 201
        assert {message["content"] for message in messages} == {
            f"Question {index}" for index in range(201)
        }


def test_question_conversation_persists_and_idempotently_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = make_user(db, "owner@qq.com")
        other = make_user(db, "other@qq.com")
        wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
        foreign_wiki = PersonalWiki(owner_id=other.id, name="Other Wiki")
        db.add_all([wiki, foreign_wiki])
        db.flush()
        db.add(
            WikiDocument(
                wiki_id=wiki.id,
                path="concepts/safety.md",
                page_id="safety",
                page_type="concept",
                title="Safety",
                content="Use the safety checklist.",
                commit_sha="a" * 40,
            )
        )
        db.commit()

        request = WikiQuestionRequest(query="How should I stay safe?", request_id="req-one")
        response = questions.ask_question(request, owner, wiki, db)
        assert questions.QuestionResponseView.model_validate(response).run_id == response["run_id"]
        repeat = questions.ask_question(request, owner, wiki, db)
        assert repeat == response
        assert response["citations"] == ["concepts/safety.md"]
        assert response["run_id"]
        assert response["message_id"]
        assert len(questions.list_conversations(owner, wiki, db)) == 1
        messages = questions.get_conversation(str(response["conversation_id"]), owner, wiki, db)[
            "messages"
        ]
        questions.ConversationView.model_validate(
            {
                "id": response["conversation_id"],
                "title": "How should I stay safe?",
                "messages": messages,
            }
        )
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert messages[1]["answer"]["message_id"] == response["message_id"]
        assert len(list(db.scalars(select(QuestionRun)))) == 1
        run_view = questions.get_question_run(str(response["run_id"]), owner, wiki, db)
        assert run_view["status"] == "completed"
        assert questions.resume_question(str(response["run_id"]), owner, wiki, db) == response

        followup = questions.ask_question(
            WikiQuestionRequest(
                query="What about that?",
                conversation_id=str(response["conversation_id"]),
                request_id="req-two",
            ),
            owner,
            wiki,
            db,
        )
        assert followup["conversation_id"] == response["conversation_id"]
        assert (
            len(
                questions.get_conversation(str(response["conversation_id"]), owner, wiki, db)[
                    "messages"
                ]
            )
            == 4
        )
        assert questions.list_conversations(other, foreign_wiki, db) == []
        with pytest.raises(HTTPException) as denied:
            questions.get_conversation(str(response["conversation_id"]), other, foreign_wiki, db)
        assert denied.value.status_code == 404
        with pytest.raises(HTTPException) as denied_run:
            questions.get_question_run(str(response["run_id"]), other, foreign_wiki, db)
        assert denied_run.value.status_code == 404
        with pytest.raises(HTTPException) as denied_delete:
            questions.delete_conversation(str(response["conversation_id"]), other, foreign_wiki, db)
        assert denied_delete.value.status_code == 404
        questions.delete_conversation(str(response["conversation_id"]), owner, wiki, db)
        assert db.get(Conversation, str(response["conversation_id"])) is None
        assert db.get(QuestionRun, str(response["run_id"])) is None


def test_failed_answer_resumes_using_saved_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = make_user(db, "owner@qq.com")
        wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
        db.add(wiki)
        db.flush()
        conversation = questions.Conversation(user_id=owner.id, wiki_id=wiki.id, title="Safety")
        db.add(conversation)
        db.flush()
        run = QuestionRun(
            user_id=owner.id,
            wiki_id=wiki.id,
            conversation_id=conversation.id,
            request_id="retry",
            model_profile_id="answer",
            question="Safety?",
            status="failed",
            stage="answering",
            standalone_question="Safety procedures",
            evidence_json=[
                {
                    "document_id": "concepts/safety.md",
                    "score": 1.0,
                    "channels": ["bm25"],
                    "content": "Use the safety checklist.",
                    "metadata": {"layer": "wiki"},
                }
            ],
        )
        db.add(run)
        db.commit()

        result = questions.resume_question(run.id, owner, wiki, db)

        assert result["answer"] == "Use the safety checklist."
        assert db.get(QuestionRun, run.id).status == "completed"
        assert db.scalar(select(ConversationMessage.id)) is not None


def test_superseded_run_cannot_save_progress() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = make_user(db, "owner@qq.com")
        wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
        db.add(wiki)
        db.flush()
        conversation = Conversation(user_id=owner.id, wiki_id=wiki.id, title="Safety")
        db.add(conversation)
        db.flush()
        run = QuestionRun(
            user_id=owner.id,
            wiki_id=wiki.id,
            conversation_id=conversation.id,
            request_id="one",
            question="Safety?",
            model_profile_id="answer",
            status="running",
            stage="retrieving",
        )
        db.add(run)
        db.commit()
        previous_lease = run.lease_token
        run.lease_token = "new-lease"
        db.commit()

        with pytest.raises(questions.RunSuperseded):
            questions._save_progress(db, run, previous_lease, "answering", [], [])


def test_answer_failure_retains_checkpoint_for_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStructured(FakeStructured):
        def invoke(self, messages: object) -> object:
            if self.schema is AnswerResult:
                raise RuntimeError("provider failed")
            return super().invoke(messages)

    class BrokenModel(FakeModel):
        def with_structured_output(self, schema: type, *, method: str) -> BrokenStructured:
            assert method == "function_calling"
            return BrokenStructured(schema)

    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", BrokenModel())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = make_user(db, "owner@qq.com")
        wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
        db.add(wiki)
        db.flush()
        db.add(
            WikiDocument(
                wiki_id=wiki.id,
                path="concepts/safety.md",
                page_id="safety",
                page_type="concept",
                title="Safety",
                content="Use the safety checklist.",
                commit_sha="a" * 40,
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as error:
            questions.ask_question(
                WikiQuestionRequest(query="Safety?", request_id="recover-once"),
                owner,
                wiki,
                db,
            )
        assert error.value.status_code == 503
        run_id = error.value.detail["run_id"]
        failed = db.get(QuestionRun, run_id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.stage == "answering"
        assert failed.evidence_json[0]["document_id"] == "concepts/safety.md"

        monkeypatch.setattr(FakeModelFactory, "model", FakeModel())
        recovered = questions.resume_question(run_id, owner, wiki, db)
        assert recovered["run_id"] == run_id
        assert recovered["citations"] == ["concepts/safety.md"]
        assert len(list(db.scalars(select(ConversationMessage)))) == 2


def test_question_route_compacts_prior_long_answer_without_changing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", FakeModel())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner = make_user(db, "owner@qq.com")
        wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
        db.add(wiki)
        db.flush()
        conversation = Conversation(
            user_id=owner.id,
            wiki_id=wiki.id,
            title="Safety",
        )
        db.add(conversation)
        db.flush()
        long_answer = "请查看安全检查清单。" * 800
        previous = ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=long_answer,
        )
        db.add(previous)
        db.add(
            WikiDocument(
                wiki_id=wiki.id,
                path="concepts/safety.md",
                page_id="safety",
                page_type="concept",
                title="Safety",
                content="Use the safety checklist.",
                commit_sha="a" * 40,
            )
        )
        db.commit()

        result = questions.ask_question(
            WikiQuestionRequest(
                query="它包含哪些步骤？",
                conversation_id=conversation.id,
                request_id="long-history",
            ),
            owner,
            wiki,
            db,
        )
        db.refresh(previous)
        db.refresh(conversation)

        assert result["citations"] == ["concepts/safety.md"]
        assert previous.content == long_answer
        assert conversation.memory_cursor_id == previous.id
        assert conversation.topic_memory == "用户正在询问安全检查清单。"
