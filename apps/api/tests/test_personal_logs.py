import asyncio
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response
from wiki_agent import main
from wiki_agent.api.logs import list_logs
from wiki_agent.db import Base
from wiki_agent.models import (
    AgentRun,
    ApiErrorLog,
    AuditLog,
    Conversation,
    PersonalWiki,
    QuestionRun,
    User,
)


def test_personal_logs_are_scoped_to_the_current_user_and_wiki() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(email="owner@qq.com", display_name="Owner", password_hash="unused")
        other_user = User(email="other@qq.com", display_name="Other", password_hash="unused")
        db.add_all([user, other_user])
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Wiki")
        other_wiki = PersonalWiki(owner_id=other_user.id, name="Other Wiki")
        db.add_all([wiki, other_wiki])
        db.flush()

        operation = AuditLog(
            actor_id=user.id,
            action="source.uploaded",
            resource_type="source",
            details={"name": "notes.txt"},
        )
        db.add(operation)
        db.flush()
        operation_id = operation.id
        db.add_all(
            [
                AuditLog(
                    actor_id=other_user.id,
                    action="source.deleted",
                    resource_type="source",
                ),
                AuditLog(
                    actor_id=user.id,
                    action="wiki.index_failed",
                    resource_type="wiki_commit",
                    details={"error": "Index unavailable"},
                ),
                ApiErrorLog(
                    user_id=user.id,
                    method="POST",
                    path="/api/v1/sources",
                    error_type="RuntimeError",
                    message="Upload failed",
                ),
                ApiErrorLog(
                    user_id=other_user.id,
                    method="POST",
                    path="/api/v1/sources",
                    error_type="RuntimeError",
                    message="Private error",
                ),
                AgentRun(
                    wiki_id=wiki.id,
                    created_by_id=user.id,
                    kind="compile",
                    status="failed",
                    error="Compilation failed",
                ),
                AgentRun(
                    wiki_id=other_wiki.id,
                    created_by_id=other_user.id,
                    kind="compile",
                    status="failed",
                    error="Other user's failure",
                ),
            ]
        )
        db.flush()
        conversation = Conversation(
            user_id=user.id,
            wiki_id=wiki.id,
            title="Question",
        )
        db.add(conversation)
        db.flush()
        db.add_all(
            [
            QuestionRun(
                conversation_id=conversation.id,
                wiki_id=wiki.id,
                user_id=user.id,
                request_id="request-1",
                model_profile_id="model",
                question="How do I import a file?",
                status="failed",
                error="Answer failed",
            ),
            QuestionRun(
                conversation_id=conversation.id,
                wiki_id=wiki.id,
                user_id=user.id,
                request_id="request-2",
                model_profile_id="model",
                question="Where are the imported files?",
                status="completed",
                answer_json={"answer": "Open the File Import page."},
            ),
            ]
        )
        db.commit()

        all_logs = list_logs(
            user,
            wiki,
            db,
            category="all",
            source="all",
            date_from=None,
            date_to=None,
            search=None,
            offset=0,
            limit=20,
        )
        api_logs = list_logs(
            user,
            wiki,
            db,
            category="error",
            source="API 接口",
            date_from=None,
            date_to=None,
            search="Upload",
            offset=0,
            limit=20,
        )
        first_page = list_logs(
            user,
            wiki,
            db,
            category="all",
            source="all",
            date_from=None,
            date_to=None,
            search=None,
            offset=0,
            limit=1,
        )
        future_logs = list_logs(
            user,
            wiki,
            db,
            category="all",
            source="all",
            date_from=date.today() + timedelta(days=1),
            date_to=None,
            search=None,
            offset=0,
            limit=20,
        )
        successful_questions = list_logs(
            user,
            wiki,
            db,
            category="operation",
            source="知识问答",
            date_from=None,
            date_to=None,
            search="imported files",
            offset=0,
            limit=20,
        )
        index_failures = list_logs(
            user,
            wiki,
            db,
            category="error",
            source="知识库",
            date_from=None,
            date_to=None,
            search=None,
            offset=0,
            limit=20,
        )

    assert all_logs["total"] == 6
    assert {entry["event"] for entry in all_logs["items"]} == {
        "source.uploaded",
        "wiki.index_failed",
        "RuntimeError",
        "compile",
        "question",
    }
    assert any(entry["id"] == operation_id for entry in all_logs["items"])
    assert all(entry["actor_name"] == "Owner" for entry in all_logs["items"])
    assert next(
        entry for entry in all_logs["items"] if entry["event"] == "wiki.index_failed"
    )["category"] == "error"
    assert api_logs["total"] == 1
    assert api_logs["items"][0]["details"] == "Upload failed"
    assert first_page["total"] == 6
    assert len(first_page["items"]) == 1
    assert future_logs["total"] == 0
    assert future_logs["items"] == []
    assert successful_questions["total"] == 1
    assert successful_questions["items"][0]["details"] == "Where are the imported files?"
    assert successful_questions["items"][0]["category"] == "operation"
    assert index_failures["total"] == 1


def test_api_error_log_is_associated_with_the_authenticated_user(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(email="owner@qq.com", display_name="Owner", password_hash="unused")
        db.add(user)
        db.commit()
        user_id = user.id

    monkeypatch.setattr(main, "SessionLocal", lambda: Session(engine))
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/sources",
            "raw_path": b"/api/v1/sources",
            "query_string": b"secret=not-for-logs",
            "headers": [(b"host", b"example.test")],
            "client": ("127.0.0.1", 12345),
            "server": ("example.test", 443),
        }
    )
    request.state.user_id = user_id
    main.record_api_error(request, "RuntimeError", "Upload failed")
    main.record_api_error(request, "HTTPError", "API returned HTTP 500")

    with Session(engine) as db:
        entry = db.query(ApiErrorLog).one()

    assert entry.user_id == user_id
    assert entry.path == "/api/v1/sources"
    assert entry.message == "Upload failed"
    assert "not-for-logs" not in entry.path


def test_failed_write_requests_are_recorded_without_logging_reads_or_question_runs(
    monkeypatch,
) -> None:
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main,
        "record_api_error",
        lambda request, error_type, message: recorded.append((request.url.path, message)),
    )

    async def call_next(_request: Request) -> Response:
        return Response(status_code=422)

    def make_request(method: str, path: str) -> Request:
        return Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "https",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [(b"host", b"example.test")],
                "client": ("127.0.0.1", 12345),
                "server": ("example.test", 443),
            }
        )

    for method, path in [
        ("POST", "/api/v1/sources"),
        ("GET", "/api/v1/sources"),
        ("POST", "/api/v1/questions"),
    ]:
        asyncio.run(main.verify_csrf(make_request(method, path), call_next))

    assert recorded == [("/api/v1/sources", "API returned HTTP 422")]
