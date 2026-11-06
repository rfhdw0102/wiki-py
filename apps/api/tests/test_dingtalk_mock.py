"""Approved SIM/local checks using real isolated SQLite and temporary Git."""

import hashlib
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from wiki_agent.api.dependencies import get_wiki_repository
from wiki_agent.config import Settings, get_settings
from wiki_agent.db import Base, get_db
from wiki_agent.integrations import dingtalk_import
from wiki_agent.integrations.dingtalk_mock import get_document, list_documents
from wiki_agent.main import app
from wiki_agent.models import AgentRun, Draft, FeatureFlag, Source, SourceRevision, SourceSpan, User
from wiki_agent.security import create_token
from wiki_agent.wiki.repository import WikiRepository

# Independent expected literals; do not generate expectations with implementation.
A_TITLE = "模拟文档A：项目说明"
A = (
    "这是模拟文档 A 的第一段，说明本地导入流程。English: Alpha only.\n\n"
    "这是模拟文档 A 的第二段，保留中文、English 和标点；顺序不得改变。"
)
B = (
    "这是模拟文档 B 的第一段，B_ONLY 内容不应出现在 A 的页面。\n\n"
    "这是模拟文档 B 的第二段，English: Beta only."
)
NOTE = "来源：模拟钉钉（未连接真实钉钉）。 [^sim-1] [^sim-2]"
C_PAGE = """---
id: existing-c
type: source
title: Existing C
created: 2026-01-01T00:00:00Z
updated: 2026-01-01T00:00:00Z
---

Existing C stays unchanged.
"""
PREFIX = "/api/v1/integrations/dingtalk"


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        _env_file=None,
        dingtalk_mode="mock",
        knowledge_root=tmp_path / "knowledge",
        storage_root=tmp_path / "storage",
        database_url=f"sqlite:///{tmp_path}/db.sqlite",
    )
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        user = User(
            id=str(uuid4()),
            email="mock@example.test",
            display_name="Mock U",
            password_hash="unused",
        )
        db.add_all([user, FeatureFlag(key="wiki_editing", enabled=True)])
        db.commit()
        user_id = user.id
    repo = WikiRepository(settings.knowledge_root)
    repo.initialize()
    repo.publish(
        {"sources/existing-c.md": C_PAGE},
        base_commit=repo.head(),
        author_name="Mock U",
        author_email="mock@example.test",
        message="Seed C",
    )

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_wiki_repository] = lambda: repo
    token = create_token(user_id, "access", settings, timedelta(minutes=15))
    client = TestClient(app)  # No lifespan: all paths/DB are explicitly isolated above.
    client.headers["Authorization"] = f"Bearer {token}"
    yield client, factory, repo, settings, user_id
    client.close()
    app.dependency_overrides.clear()
    engine.dispose()


def post(client, document="mock-a", request_id=None, **extra):
    return client.post(
        PREFIX + "/imports",
        json={"document_id": document, "request_id": request_id or str(uuid4()), **extra},
    )


def test_mock_catalog_explicit_mode_and_no_network(env, monkeypatch):
    client, _, _, settings, _ = env

    def forbidden(*args, **kwargs):
        raise AssertionError("Mock import must not open network sockets")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    assert client.get(PREFIX + "/status").json() == {
        "mode": "mock",
        "simulation": True,
        "real_connection": "not_implemented",
        "fixture_version": "fixture-v1",
    }
    catalog = client.get(PREFIX + "/documents").json()
    assert catalog["items"] == [
        {"document_id": "mock-a", "title": A_TITLE},
        {"document_id": "mock-b", "title": "模拟文档B：会议记录"},
    ]
    assert catalog["notice"] == "模拟钉钉，未连接真实钉钉"
    assert get_document("mock-a").canonical_text == A
    assert get_document("mock-b").canonical_text == B
    assert post(client).status_code == 201
    settings.dingtalk_mode = "disabled"
    assert client.get(PREFIX + "/status").json()["real_connection"] == "not_implemented"
    assert client.get(PREFIX + "/documents").status_code == 409
    assert post(client).json()["detail"]["code"] == "integration_disabled"
    with pytest.raises(ValidationError, match="真实钉钉尚未实现"):
        Settings(_env_file=None, dingtalk_mode="real")
    assert Settings(_env_file=None).dingtalk_mode == "disabled"
    assert len(list_documents()) == 2


def test_a_exact_single_published_page_db_records_and_normal_reopen(env):
    client, factory, repo, settings, _ = env
    before, old_c, old_history = (
        set(repo.list_pages()),
        repo.read_page("sources/existing-c.md"),
        repo.history("sources/existing-c.md"),
    )
    before_head = repo.head()
    request_id = str(uuid4())
    response = post(client, request_id=request_id)
    assert response.status_code == 201, response.text
    result = response.json()
    path = result["page_path"]
    assert result["state"] == "published" and result["index_status"] == "deferred"
    assert set(repo.list_pages()) - before == {path}
    assert repo._run(
        "diff", "--name-only", before_head, result["published_commit"]
    ).stdout.splitlines() == ["wiki/" + path]
    page = repo.read_page(path)
    assert page.metadata.title == A_TITLE
    assert page.body == A + "\n\n" + NOTE
    assert "B_ONLY" not in page.body
    assert set(page.metadata.tags) == {"dingtalk-mock", "simulation"}
    assert repo.read_page("sources/existing-c.md") == old_c
    assert repo.history("sources/existing-c.md") == old_history
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 1
        run = db.get(AgentRun, request_id)
        draft = db.get(Draft, run.result_json["draft_id"])
        rev = db.get(SourceRevision, result["source_revision_id"])
        spans = list(db.scalars(select(SourceSpan).order_by(SourceSpan.ordinal)))
        assert (
            draft.status.value == "published"
            and draft.published_commit == result["published_commit"]
        )
        assert set(draft.files) == {path} and run.status == "published"
        assert rev.metadata_json["origin"] == "dingtalk_mock" and rev.status == "parsed"
        assert rev.sha256 == hashlib.sha256(A.encode("utf-8")).hexdigest()
        assert (settings.storage_root / rev.object_path).read_bytes() == A.encode("utf-8")
        assert db.get(Source, rev.source_id).current_revision_id == rev.id
        assert [span.content for span in spans] == A.split("\n\n")
        assert all(c.source_revision_id == rev.id for c in page.metadata.citations.values())
    # New repository object and uncached normal authenticated GET, not an import preview.
    repo = WikiRepository(settings.knowledge_root)
    assert repo.read_page(path) == page
    assert path in client.get("/api/v1/wiki/pages").json()["pages"]
    reopened = client.get("/api/v1/wiki/pages/" + path, headers={"Cache-Control": "no-cache"})
    assert reopened.json()["body"] == page.body
    assert reopened.json()["metadata"]["title"] == A_TITLE
    settings.dingtalk_mode = "disabled"
    assert client.get(PREFIX + "/imports/" + request_id).json()["state"] == "published"
    assert client.get("/api/v1/wiki/pages/" + path).json()["body"] == page.body


def test_b_selection_exact_and_different_action_not_source_dedup(env):
    client, _, repo, _, _ = env
    first = post(client, "mock-b")
    assert first.status_code == 201, first.text
    assert repo.read_page(first.json()["page_path"]).body == B + "\n\n" + NOTE
    second = post(client, "mock-b")
    assert second.status_code == 201
    assert second.json()["page_path"] != first.json()["page_path"]


def test_replay_conflict_and_generic_draft_guard(env):
    client, factory, repo, _, user_id = env
    action = str(uuid4())
    first = post(client, request_id=action)
    assert first.status_code == 201, first.text
    head = repo.head()
    assert post(client, request_id=action).status_code == 200
    assert post(client, request_id=action).json() == first.json()
    assert post(client, "mock-b", request_id=action).status_code == 409
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 1
        draft_id = db.get(AgentRun, action).result_json["draft_id"]
        other = AgentRun(
            id=str(uuid4()), kind="compile_source", created_by_id=user_id, input_json={}
        )
        db.add(other)
        db.commit()
        other_id = other.id
    assert post(client, request_id=other_id).json()["detail"]["code"] == "request_conflict"
    assert (
        client.post(f"/api/v1/drafts/{draft_id}/publish", json={"message": "again"}).status_code
        == 409
    )
    assert repo.head() == head


def test_auth_csrf_feature_invalid_input_have_no_side_effects(env):
    client, factory, repo, settings, user_id = env
    head = repo.head()
    bearer = client.headers.pop("Authorization")
    assert client.get(PREFIX + "/status").status_code == 401
    assert client.get(PREFIX + "/documents").status_code == 401
    assert post(client).status_code == 401
    client.cookies.set("access_token", bearer.removeprefix("Bearer "))
    client.cookies.set("csrf_token", "correct")
    assert post(client).status_code == 403
    assert (
        client.post(
            PREFIX + "/imports",
            headers={"x-csrf-token": "wrong"},
            json={"document_id": "mock-a", "request_id": str(uuid4())},
        ).status_code
        == 403
    )
    client.cookies.clear()
    client.headers["Authorization"] = bearer
    for extra in [
        {"url": "https://example.test"},
        {"body": "arbitrary"},
        {"page_path": "sources/c.md"},
    ]:
        assert post(client, **extra).status_code == 422
    assert post(client, ["mock-a", "mock-b"]).status_code == 422
    assert post(client, request_id="invalid").status_code == 422
    assert post(client, "missing").status_code == 404
    with factory() as db:
        flag = db.get(FeatureFlag, "wiki_editing")
        flag.enabled = False
        db.commit()
    assert post(client).status_code == 403
    with factory() as db:
        db.get(User, user_id).is_active = False
        db.commit()
    assert client.get(PREFIX + "/documents").status_code == 401
    assert post(client).status_code in {401, 403}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert db.scalar(select(func.count()).select_from(Source)) == 0
    assert repo.head() == head


def test_other_author_request_conflict_and_get_404(env):
    client, factory, _, _, _ = env
    action = str(uuid4())
    with factory() as db:
        other = User(
            id=str(uuid4()),
            email="other@example.test",
            display_name="Other",
            password_hash="unused",
        )
        db.add(other)
        db.flush()
        db.add(
            AgentRun(id=action, kind="dingtalk_mock_import", created_by_id=other.id, input_json={})
        )
        db.commit()
    assert post(client, request_id=action).json()["detail"]["code"] == "request_conflict"
    assert client.get(PREFIX + "/imports/" + action).status_code == 404
    assert client.get(PREFIX + "/imports/" + str(uuid4())).status_code == 404


def test_concurrent_claim_only_one_executes(env):
    _, factory, repo, settings, user_id = env
    action, barrier = str(uuid4()), Barrier(2)

    def execute():
        with factory() as db:
            user = db.get(User, user_id)
            barrier.wait()
            try:
                return dingtalk_import.import_document(db, repo, settings, user, "mock-a", action)[
                    1
                ]
            except Exception as exc:
                assert exc.status_code == 409
                return False

    with ThreadPoolExecutor(2) as executor:
        assert sum(executor.map(lambda _: execute(), range(2))) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 1
        assert db.get(AgentRun, action).status == "published"


def test_pre_git_failure_records_no_page(env, monkeypatch):
    client, factory, repo, _, _ = env
    head, action = repo.head(), str(uuid4())

    def fail(*args):
        raise OSError("Do not expose sensitive exception")

    monkeypatch.setattr(dingtalk_import.SourceObjectStore, "put", fail)
    response = post(client, request_id=action)
    assert response.status_code == 500
    assert response.json()["detail"]["published"] is False
    assert "sensitive" not in response.text
    assert repo.head() == head
    with factory() as db:
        assert db.get(AgentRun, action).status == "failed"


def test_final_db_failure_known_git_residual_and_read_only_query(env, monkeypatch):
    client, factory, repo, _, _ = env
    action, original = str(uuid4()), factory.class_.commit

    def fail_final(db):
        if any(isinstance(obj, AgentRun) and obj.status == "published" for obj in db.dirty):
            raise RuntimeError("private final commit failure")
        return original(db)

    monkeypatch.setattr(factory.class_, "commit", fail_final)
    response = post(client, request_id=action)
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "published_state_not_recorded" and detail["published"] is True
    assert repo.read_page(detail["page_path"]).body == A + "\n\n" + NOTE
    head = repo.head()
    with factory() as db:
        run = db.get(AgentRun, action)
        assert run.status == "publishing"
        draft_id = run.result_json["draft_id"]
    query = client.get(PREFIX + "/imports/" + action)
    assert query.status_code == 200 and query.json()["published"] is True
    assert query.json()["state"] == "publishing"  # GET never pretends DB repaired.
    assert query.json()["published_commit"] == detail["published_commit"]
    assert post(client, request_id=action).status_code == 409
    assert (
        client.post(f"/api/v1/drafts/{draft_id}/publish", json={"message": "again"}).status_code
        == 409
    )
    assert repo.head() == head
    with factory() as db:
        assert db.get(AgentRun, action).status == "publishing"
        assert db.scalar(select(func.count()).select_from(Source)) == 1


def test_unknown_publication_never_reexecutes(env, monkeypatch):
    client, factory, repo, _, _ = env
    head, action = repo.head(), str(uuid4())

    def fail(*args, **kwargs):
        raise RuntimeError("Publish boundary unknown")

    monkeypatch.setattr(repo, "publish", fail)
    response = post(client, request_id=action)
    assert response.status_code == 503
    assert response.json()["detail"]["published"] is None
    assert response.json()["detail"]["code"] == "publication_outcome_unknown"
    assert post(client, request_id=action).status_code == 409
    assert client.get(PREFIX + "/imports/" + action).json()["published"] is None
    assert repo.head() == head
    with factory() as db:
        assert db.get(AgentRun, action).status == "outcome_unknown"
        assert db.scalar(select(func.count()).select_from(Source)) == 1


def test_generic_tasks_refuse_mock_run_before_mutation(env):
    from wiki_agent.tasks import (
        _compile_source,
        _crawl_site,
        _parse_source_run,
        _prepare_compilation_source,
    )

    client, factory, _, _, _ = env
    action = str(uuid4())
    assert post(client, request_id=action).status_code == 201
    with factory() as db:
        for task in [_compile_source, _crawl_site, _parse_source_run, _prepare_compilation_source]:
            with pytest.raises(ValueError, match="模拟钉钉动作"):
                task(db, action)
        assert db.get(AgentRun, action).status == "published"
