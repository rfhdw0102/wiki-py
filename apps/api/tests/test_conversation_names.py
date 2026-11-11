import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from test_question_conversations import FakeModel, FakeModelFactory, FakeStructured
from test_question_lifecycle import seed_wiki
from wiki_agent.api import questions
from wiki_agent.api.dependencies import get_current_user, get_current_wiki
from wiki_agent.api.schemas import WikiQuestionRequest
from wiki_agent.db import Base, ensure_conversation_title_schema, get_db
from wiki_agent.models import AuditLog, Conversation, ConversationMessage, QuestionRun
from wiki_agent.qa.agent import ConversationTitle


@pytest.mark.parametrize(
    "title", ["", "   ", "x" * 121, "a\nb", "a\rb", "a\x00b", "a\x7fb", "a\u2028b"]
)
def test_conversation_name_rejects_empty_long_or_multiline_names(title: str) -> None:
    with pytest.raises(ValidationError):
        questions.ConversationRename(title=title)


def test_rename_persists_and_is_visible_in_all_question_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", FakeModel())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        payload = WikiQuestionRequest(query="Safety?", request_id="auto-conversation")
        response = questions.ask_question(payload, owner, wiki, db)
        conversation_id = str(response["conversation_id"])
        assert len(questions.list_conversations(owner, wiki, db)) == 1
        before = [(message.content, message.citations) for message in db.scalars(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation_id
            )
        )]
        renamed = questions.rename_conversation(
            conversation_id, questions.ConversationRename(title="  我的安全资料  "), owner, wiki, db
        )
        assert renamed.title == "我的安全资料"
        assert renamed.title_is_custom is True
        assert questions.get_conversation(conversation_id, owner, wiki, db)[
            "title"
        ] == renamed.title
        assert questions.list_conversations(owner, wiki, db)[0]["title"] == renamed.title
        assert questions.get_question_run(str(response["run_id"]), owner, wiki, db)[
            "conversation_title"
        ] == renamed.title
        assert questions.ask_question(payload, owner, wiki, db)[
            "conversation_title"
        ] == renamed.title
        detail = questions.get_conversation(conversation_id, owner, wiki, db)
        assert detail["messages"][1]["answer"]["conversation_title"] == renamed.title
        assert [(message.content, message.citations) for message in db.scalars(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation_id
            )
        )] == before
        audit = db.scalar(select(AuditLog).where(AuditLog.action == "conversation.renamed"))
        assert audit is not None and audit.details["title"] == renamed.title
        assert db.get(QuestionRun, response["run_id"]).id == response["run_id"]
    with Session(engine) as db:
        assert db.get(Conversation, conversation_id).title == "我的安全资料"


def test_manual_placeholder_name_is_not_replaced_on_first_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ManualTitleModel(FakeModel):
        def with_structured_output(self, schema: type, *, method: str) -> FakeStructured:
            assert schema is not ConversationTitle, "manually assigned title must be retained"
            return super().with_structured_output(schema, method=method)

    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", ManualTitleModel())
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        conversation = questions.create_conversation(owner, wiki, db)
        questions.rename_conversation(
            conversation.id, questions.ConversationRename(title="新对话"), owner, wiki, db
        )
        response = questions.ask_question(
            WikiQuestionRequest(query="Safety?", conversation_id=conversation.id),
            owner, wiki, db,
        )
        assert response["conversation_title"] == "新对话"


def test_manual_rename_during_title_generation_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class ConcurrentTitle(FakeStructured):
        def invoke(self, messages: object) -> object:
            if self.schema is ConversationTitle:
                with Session(engine) as control:
                    run = control.scalar(select(QuestionRun))
                    assert run is not None
                    questions.rename_conversation(
                        run.conversation_id, questions.ConversationRename(title="手动名称"),
                        owner, wiki, control,
                    )
            return super().invoke(messages)

    class ConcurrentTitleModel(FakeModel):
        def with_structured_output(self, schema: type, *, method: str) -> ConcurrentTitle:
            return ConcurrentTitle(schema)

    monkeypatch.setattr(questions, "ModelFactory", FakeModelFactory)
    monkeypatch.setattr(FakeModelFactory, "model", ConcurrentTitleModel())
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        response = questions.ask_question(WikiQuestionRequest(query="Safety?"), owner, wiki, db)
        assert response["conversation_title"] == "手动名称"
        assert db.get(Conversation, response["conversation_id"]).title_is_custom is True


def test_rename_requires_owned_conversation_and_current_wiki() -> None:
    from test_question_conversations import make_user
    from wiki_agent.models import PersonalWiki

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        owner, wiki = seed_wiki(db)
        other = make_user(db, "other@qq.com")
        second_wiki = PersonalWiki(owner_id=owner.id, name="Second")
        db.add(second_wiki)
        db.commit()
        conversation = questions.create_conversation(owner, wiki, db)
        for user, scope in [(other, wiki), (owner, second_wiki)]:
            with pytest.raises(questions.HTTPException) as denied:
                questions.rename_conversation(
                    conversation.id, questions.ConversationRename(title="not allowed"),
                    user, scope, db,
                )
            assert denied.value.status_code == 404
        assert db.scalar(select(AuditLog)) is None


def test_existing_conversation_schema_is_upgraded_without_changing_titles() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE conversations (id TEXT, title TEXT)"))
        connection.execute(text("INSERT INTO conversations VALUES ('one','Existing')"))
    ensure_conversation_title_schema(engine)
    ensure_conversation_title_schema(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT id,title,title_is_custom FROM conversations")).one()
        assert row == ("one", "Existing", 0)


def test_conversation_patch_http_contract_and_validation() -> None:
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        owner, wiki = seed_wiki(db)
        conversation = questions.create_conversation(owner, wiki, db)

    def session():
        with Session(engine) as db:
            yield db

    app = FastAPI()
    app.include_router(questions.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: owner
    app.dependency_overrides[get_current_wiki] = lambda: wiki
    path = f"/api/v1/questions/conversations/{conversation.id}"
    with TestClient(app) as client:
        invalid = client.patch(path, json={"title": " "})
        assert invalid.status_code == 422
        updated = client.patch(path, json={"title": "  手动标题  "})
        assert updated.status_code == 200
        assert updated.json()["title"] == "手动标题"
        assert client.get(path).json()["title"] == "手动标题"
