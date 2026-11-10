from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from test_question_conversations import FakeModel, FakeModelFactory, FakeStructured, make_user
from wiki_agent.api import questions
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.api.schemas import WikiQuestionRequest
from wiki_agent.db import Base, ensure_question_checkpoint_schema, get_db
from wiki_agent.models import (
    Conversation,
    ConversationMessage,
    FeatureFlag,
    PersonalWiki,
    QuestionRun,
    User,
    WikiDocument,
)
from wiki_agent.qa.agent import ConversationQuestionPlan, ConversationTitle


def seed_wiki(db: Session) -> tuple[User, PersonalWiki]:
    owner = make_user(db, "owner@qq.com")
    wiki = PersonalWiki(owner_id=owner.id, name="Owner Wiki")
    db.add(wiki)
    db.flush()
    db.add(
        WikiDocument(
            wiki_id=wiki.id, path="concepts/safety.md", page_id="safety",
            page_type="concept", title="Safety", content="Use the safety checklist.",
            commit_sha="a" * 40,
        )
    )
    db.commit()
    return owner, wiki


def test_title_and_request_lookup_are_available_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    seen: list[dict[str, object]] = []

    class ObserveModel(FakeModel):
        def invoke(self, messages: object) -> AIMessage:
            with Session(engine) as observer:
                view = questions.get_question_request("title-now", owner, wiki, observer)
                assert view is not None
                questions.QuestionRunView.model_validate(view)
                assert view["conversation_title"] == "Safety procedures"
                assert view["resumable"] is False
                seen.append(view)
            return super().invoke(messages)

    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", ObserveModel())
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        assert questions.get_question_request("unknown", owner, wiki, db) is None
        result = questions.ask_question(
            WikiQuestionRequest(query="How do I stay safe?", request_id="title-now"),
            owner, wiki, db,
        )
        assert len(seen) == 2
        assert result["conversation_title"] == "Safety procedures"
        second = make_user(db, "other@qq.com")
        db.commit()
        assert questions.get_question_request("title-now", second, wiki, db) is None


def test_cancel_invalidates_inflight_execution_and_cannot_be_resumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    cancelled: list[dict[str, object]] = []

    class CancelModel(FakeModel):
        def invoke(self, messages: object) -> AIMessage:
            with Session(engine) as control:
                current = questions.get_question_request("stop-now", owner, wiki, control)
                assert current is not None
                view = questions.cancel_question(str(current["id"]), owner, wiki, control)
                cancelled.append(view)
            return super().invoke(messages)

    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", CancelModel())
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        with pytest.raises(HTTPException) as stopped:
            questions.ask_question(
                WikiQuestionRequest(query="Safety?", request_id="stop-now"), owner, wiki, db,
            )
        assert stopped.value.status_code == 409
        assert stopped.value.detail["message"] == "Question run was cancelled"
        run_id = str(cancelled[0]["id"])
        view = questions.get_question_run(run_id, owner, wiki, db)
        assert view["status"] == view["stage"] == "cancelled"
        assert view["resumable"] is False
        assert view["answer"] is None
        assert view["tool_trace"] == []
        assert db.scalar(select(ConversationMessage.id)) is None
        with pytest.raises(HTTPException, match="Question cannot be resumed"):
            questions.resume_question(run_id, owner, wiki, db)
        assert questions.cancel_question(run_id, owner, wiki, db)["status"] == "cancelled"


def test_cancellation_is_scoped_and_does_not_destroy_completed_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", FakeModel())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        result = questions.ask_question(
            WikiQuestionRequest(query="Safety?", request_id="complete"), owner, wiki, db,
        )
        run_id = str(result["run_id"])
        other = make_user(db, "other@qq.com")
        db.commit()
        with pytest.raises(HTTPException) as denied:
            questions.cancel_question(run_id, other, wiki, db)
        assert denied.value.status_code == 404
        view = questions.cancel_question(run_id, owner, wiki, db)
        assert view["status"] == "completed"
        assert view["resumable"] is False
        assert view["answer"] == result


def test_late_provider_failure_cannot_recreate_a_deleted_cancelled_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class DeleteModel(FakeModel):
        def invoke(self, messages: object) -> AIMessage:
            with Session(engine) as control:
                current = questions.get_question_request("stop-delete", owner, wiki, control)
                assert current is not None
                questions.cancel_question(str(current["id"]), owner, wiki, control)
                questions.delete_conversation(str(current["conversation_id"]), owner, wiki, control)
            raise ConnectionError("late provider failure")

    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", DeleteModel())
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        with pytest.raises(HTTPException) as stopped:
            questions.ask_question(
                WikiQuestionRequest(query="Safety?", request_id="stop-delete"), owner, wiki, db,
            )
        assert stopped.value.status_code == 409
        assert stopped.value.detail["message"] == "Question run was removed"
        assert db.scalar(select(QuestionRun.id)) is None
        assert db.scalar(select(Conversation.id)) is None


def test_failed_retrieval_resumes_checkpoint_without_title_resolution_or_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptedModel(FakeModel):
        def invoke(self, messages: object) -> AIMessage:
            if self.count == 1:
                raise ConnectionError("provider disconnected after search")
            return super().invoke(messages)

    class ResumeModel(FakeModel):
        def __init__(self) -> None:
            super().__init__()
            self.count = 1

        def with_structured_output(self, schema: type, *, method: str) -> FakeStructured:
            assert schema not in {ConversationTitle, ConversationQuestionPlan}
            return super().with_structured_output(schema, method=method)

    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", InterruptedModel())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        with pytest.raises(HTTPException) as failed:
            questions.ask_question(
                WikiQuestionRequest(query="Safety?", request_id="resume-search"), owner, wiki, db,
            )
        run_id = failed.value.detail["run_id"]
        view = questions.get_question_run(run_id, owner, wiki, db)
        assert view["resumable"] is True
        run = db.get(QuestionRun, run_id)
        assert run is not None
        assert run.checkpoint_json["agent"]["model_steps"] == 1
        assert len(run.tool_trace) == 1

        def no_tools(*args: object, **kwargs: object) -> None:
            raise AssertionError("completed search must not run again")

        monkeypatch.setattr(questions.KnowledgeTools, "call", no_tools)
        monkeypatch.setattr(FakeModelFactory, "model", ResumeModel())
        resumed = questions.resume_question(run_id, owner, wiki, db)
        assert resumed["run_id"] == run_id
        assert resumed["answer"] == "Use the safety checklist."
        assert resumed["conversation_title"] == "Safety procedures"
        assert len(db.get(QuestionRun, run_id).tool_trace) == 1
        assert len(list(db.scalars(select(ConversationMessage)))) == 2


def test_stale_run_is_resumable_but_resume_rejects_another_active_question() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        conversation = Conversation(user_id=owner.id, wiki_id=wiki.id, title="Safety")
        db.add(conversation)
        db.flush()
        run = QuestionRun(
            user_id=owner.id, wiki_id=wiki.id, conversation_id=conversation.id,
            request_id="stale", model_profile_id="answer", question="Safety?",
            updated_at=datetime.now(UTC) - timedelta(minutes=6),
        )
        other = QuestionRun(
            user_id=owner.id, wiki_id=wiki.id, conversation_id=conversation.id,
            request_id="active", model_profile_id="answer", question="Details?",
        )
        db.add_all([run, other])
        db.commit()
        assert questions.get_question_run(run.id, owner, wiki, db)["resumable"] is True
        assert questions.get_question_run(other.id, owner, wiki, db)["resumable"] is False
        with pytest.raises(HTTPException) as conflict:
            questions.resume_question(run.id, owner, wiki, db)
        assert conflict.value.status_code == 409
        assert conflict.value.detail["message"] == "Another question is running"


def test_automatic_schema_upgrade_preserves_existing_runs() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE question_runs (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO question_runs (id) VALUES ('existing')"))
    ensure_question_checkpoint_schema(engine)
    ensure_question_checkpoint_schema(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT id, checkpoint_json FROM question_runs")).one()
        assert row == ("existing", "{}")


def test_http_cancellation_is_available_during_synchronous_question_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'questions.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    started, release = Event(), Event()

    class WaitingModel(FakeModel):
        def invoke(self, messages: object) -> AIMessage:
            started.set()
            assert release.wait(10), "test did not release the provider response"
            return super().invoke(messages)

    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", WaitingModel())
    with Session(engine, expire_on_commit=False) as db:
        owner, wiki = seed_wiki(db)
        db.add(FeatureFlag(key="wiki_questions", enabled=True))
        db.commit()

    def session():
        with Session(engine, expire_on_commit=False) as db:
            yield db

    app = FastAPI()
    app.include_router(questions.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_current_wiki] = lambda: wiki
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(
            client.post, "/api/v1/questions",
            json={"query": "Safety?", "request_id": "http-stop"},
        )
        try:
            assert started.wait(10), "question did not reach the provider"
            lookup = client.get("/api/v1/questions/requests/http-stop")
            assert lookup.status_code == 200
            run = lookup.json()
            assert run["conversation_title"] == "Safety procedures"
            stopped = client.post(f"/api/v1/questions/runs/{run['id']}/cancel")
            assert stopped.status_code == 200
            assert stopped.json()["status"] == "cancelled"
            assert stopped.json()["resumable"] is False
            assert client.post(f"/api/v1/questions/runs/{run['id']}/resume").status_code == 409
            assert client.get("/api/v1/questions/requests/unknown").json() is None
        finally:
            release.set()
        response = request.result(timeout=10)
        assert response.status_code == 409
        assert response.json()["detail"]["message"] == "Question run was cancelled"
        with Session(engine) as db:
            assert db.scalar(select(ConversationMessage.id)) is None
