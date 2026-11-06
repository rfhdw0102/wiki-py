from __future__ import annotations

import json
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import wiki_agent.compilation as compilation
import wiki_agent.tasks as tasks
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import RunnableBinding
from langchain_openai import ChatOpenAI
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from wiki_agent.compilation import enqueue_ingested_revision
from wiki_agent.config import Settings
from wiki_agent.db import Base
from wiki_agent.llm.compiler import (
    CompilationOutputError,
    CompilationProposal,
    StructuredPage,
    WikiCompiler,
    _invalid_compilation_output,
)
from wiki_agent.models import (
    AgentRun,
    Draft,
    FeatureFlag,
    ModelAssignment,
    ModelKind,
    ModelProfile,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    UserRole,
)
from wiki_agent.tasks import _change_ratio
from wiki_agent.wiki import WikiPage
from wiki_agent.wiki.schema import Citation


def source_page(revision_id: str) -> str:
    return f"""---
id: {revision_id}
type: source
title: Source summary
created: 2026-09-21T00:00:00Z
updated: 2026-09-21T00:00:00Z
sources: []
related: []
citations:
  claim:
    source_revision_id: {revision_id}
    span_id: {revision_id}:0
tags: []
status: published
---

Preserved model X-100 at 12.5 V.[^claim]
"""


class FakeStructuredModel:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.messages: list[object] = []

    def invoke(self, messages: list[object]) -> dict[str, object]:
        self.messages = messages
        return self.response


class FakeChatModel:
    def __init__(self, response: dict[str, object]) -> None:
        self.structured = FakeStructuredModel(response)

    def with_structured_output(
        self, schema: object, *, method: str | None = None
    ) -> FakeStructuredModel:
        assert method == "function_calling"
        return self.structured


def test_openai_compatible_structured_output_binds_tools_not_response_format() -> None:
    model = ChatOpenAI(
        model="compatible",
        base_url="http://127.0.0.1:9/v1",
        api_key="test-only",
    )
    bound = model.with_structured_output(CompilationProposal, method="function_calling")

    assert isinstance(bound.first, RunnableBinding)
    assert "tools" in bound.first.kwargs
    assert "response_format" not in bound.first.kwargs


def test_compiler_requires_revision_summary_and_receives_existing_content() -> None:
    revision_id = "revision-1"
    fake_model = FakeChatModel(
        {
            "summary": "Compiled source",
            "files": [
                {
                    "path": f"sources/{revision_id}.md",
                    "content": source_page(revision_id),
                }
            ],
            "conflicts": [],
        }
    )
    compiler = WikiCompiler(cast(BaseChatModel, cast(Any, fake_model)))

    proposal = compiler.compile(
        source_revision_id=revision_id,
        source_markdown="Model X-100 uses 12.5 V.",
        schema="Preserve model numbers and measurements.",
        existing_pages={
            "entities/model-x-100.md": "Existing entity property: vendor Example.",
            "sources/revision-old.md": source_page("revision-old"),
        },
    )

    assert proposal.files[0].path == f"sources/{revision_id}.md"
    prompt = str(fake_model.structured.messages[-1].content)
    assert "Existing entity property: vendor Example." in prompt
    assert f"Required source summary path: sources/{revision_id}.md" in prompt
    assert "citations:\n  fact:" in prompt
    assert f"span_id: {revision_id}:0" in prompt
    assert "frontmatter" in str(fake_model.structured.messages[0].content)
    assert 'Available source summary paths: ["sources/revision-1.md", ' in prompt
    assert '"sources/revision-old.md"]' in prompt
    instructions = str(fake_model.structured.messages[0].content)
    assert (
        "The current source summary counts even though it is created in this proposal"
        in instructions
    )
    assert "Reuse exact source_revision_id and span_id pairs" in instructions
    assert "Multiple spans or entity pages from one source do not count" in instructions
    assert "Do not prepend wiki/, knowledge/, ./, or /" in instructions
    assert "bare revision IDs in sources or related" in instructions
    assert "[a-z0-9][a-z0-9-]{1,127}" in instructions


def test_deepseek_compilation_builds_wiki_frontmatter_from_structured_fields() -> None:
    requests: list[dict[str, object]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        proposal = {
            "summary": "Source summary",
            "files": [{
                "path": "sources/revision-1.md",
                "title": "Sample",
                "sources": [],
                "related": [],
                "citations": {
                    "fact": {
                        "source_revision_id": "revision-1",
                        "span_id": "revision-1:0",
                    }
                },
                "tags": [],
                "status": "published",
                "body": "Supported claim.[^fact]",
            }],
            "conflicts": [],
        }
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(proposal)},
                }],
            },
        )

    model = ChatOpenAI(
        model="deepseek-flash",
        api_key="test-only",
        base_url="https://api.deepseek.com",
        http_client=httpx.Client(transport=httpx.MockTransport(reply)),
    )
    proposal = WikiCompiler(model).compile(
        source_revision_id="revision-1",
        source_markdown='<SOURCE_SPAN id="revision-1:0">\nSupported claim.\n</SOURCE_SPAN>',
        schema="Cite each claim.",
        existing_pages={"sources/revision-old.md": source_page("revision-old")},
    )
    page = WikiPage.from_markdown(proposal.files[0].content)
    assert page.metadata.id == "revision-1"
    assert page.metadata.citations["fact"].span_id == "revision-1:0"
    assert proposal.files[0].content.startswith("---\n")
    assert len(requests) == 1
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in requests[0]
    assert "tool_choice" not in requests[0]
    prompt_schema = json.dumps(requests[0]["messages"][0])
    assert "title" in prompt_schema
    assert "citations" in prompt_schema
    assert '"metadata"' not in prompt_schema
    prompt = str(requests[0]["messages"])
    assert "Available source summary paths" in prompt
    assert "sources/revision-old.md" in prompt
    assert "The current source summary counts even though it is created in this proposal" in prompt
    assert "Reuse exact source_revision_id and span_id pairs" in prompt
    assert "Do not prepend wiki/, knowledge/, ./, or /" in prompt
    assert "bare revision IDs in sources or related" in prompt
    assert "[a-z0-9][a-z0-9-]{1,127}" in prompt


def test_deepseek_compiler_reports_missing_title_without_leaking_output() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [{
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({
                            "summary": "Generated",
                            "files": [{
                                "path": "sources/revision-1.md",
                                "content": "SECRET_TEST_MARKER",
                            }],
                            "conflicts": [],
                        }),
                    },
                }],
            },
        )

    model = ChatOpenAI(
        model="deepseek-flash",
        api_key="test-only",
        base_url="https://api.deepseek.com",
        http_client=httpx.Client(transport=httpx.MockTransport(reply)),
    )
    with pytest.raises(CompilationOutputError, match="metadata") as failure:
        WikiCompiler(model).compile(
            source_revision_id="revision-1",
            source_markdown="text",
            schema="Cite each claim.",
            existing_pages={},
        )
    assert "SECRET_TEST_MARKER" not in str(failure.value)


def test_structured_page_preserves_existing_identity_and_server_dates() -> None:
    existing = source_page("revision-1")
    now = datetime(2026, 9, 28, tzinfo=UTC)
    file = StructuredPage.model_validate({
        "path": "sources/revision-1.md",
        "metadata": {
            "id": "ignored-model-id",
            "type": "entity",
            "created": "2099-01-01T00:00:00Z",
            "title": "Updated summary",
            "citations": {
                "claim": {
                    "source_revision_id": "revision-1",
                    "span_id": "revision-1:0",
                }
            },
        },
        "body": "Updated claim.[^claim]",
    })

    page = file.to_wiki_page(
        existing, source_revision_id="revision-1", now=now
    )
    assert page.metadata.id == "revision-1"
    assert page.metadata.type.value == "source"
    assert page.metadata.created == WikiPage.from_markdown(existing).metadata.created
    assert page.metadata.updated == now
    assert page.metadata.title == "Updated summary"


def test_structured_source_summary_requires_a_real_citation() -> None:
    file = StructuredPage(
        path="sources/revision-1.md",
        title="Summary",
        body="Unsupported claim.",
    )
    with pytest.raises(CompilationOutputError, match="no source-span citations"):
        file.to_wiki_page(
            None, source_revision_id="revision-1",
            now=datetime(2026, 9, 28, tzinfo=UTC),
        )


def test_concept_can_combine_current_and_existing_source_evidence() -> None:
    file = StructuredPage(
        path="concepts/harness-runtime.md",
        title="Harness runtime",
        sources=["sources/revision-old.md", "sources/revision-1.md"],
        citations={
            "old": Citation(source_revision_id="revision-old", span_id="revision-old:0"),
            "new": Citation(source_revision_id="revision-1", span_id="revision-1:0"),
        },
        body="A shared observation.[^old][^new]",
    )
    page = file.to_wiki_page(
        None, source_revision_id="revision-1",
        now=datetime(2026, 9, 28, tzinfo=UTC),
    )
    restored = WikiPage.from_markdown(page.to_markdown())
    assert restored.metadata.sources == ["sources/revision-1.md", "sources/revision-old.md"]
    assert {citation.source_revision_id for citation in restored.metadata.citations.values()} == {
        "revision-1", "revision-old",
    }


def test_compiler_reports_invalid_markdown_without_storing_model_text() -> None:
    fake_model = FakeChatModel(
        {
            "summary": "Generated pages",
            "files": [
                {
                    "path": "sources/revision-1.md",
                    "content": "# SECRET_TEST_MARKER plain Markdown without frontmatter",
                }
            ],
            "conflicts": [],
        }
    )
    compiler = WikiCompiler(cast(BaseChatModel, cast(Any, fake_model)))

    with pytest.raises(CompilationOutputError, match="YAML frontmatter") as failure:
        compiler.compile(
            source_revision_id="revision-1", source_markdown="text",
            schema="Create a source summary.", existing_pages={},
        )
    assert "SECRET_TEST_MARKER" not in str(failure.value)


def test_compiler_sanitizes_parser_error_containing_generated_text() -> None:
    class FailingStructured:
        def invoke(self, messages: object) -> None:
            raise OutputParserException(
                "Failed to parse generated text: SECRET_TEST_MARKER. "
                "Got: Wiki page must start with YAML frontmatter"
            )

    class FailingModel:
        def with_structured_output(self, schema: object, *, method: str) -> FailingStructured:
            return FailingStructured()

    compiler = WikiCompiler(cast(BaseChatModel, cast(Any, FailingModel())))
    with pytest.raises(CompilationOutputError, match="YAML frontmatter") as failure:
        compiler.compile(
            source_revision_id="revision-1", source_markdown="text",
            schema="Create a source summary.", existing_pages={},
        )
    assert "SECRET_TEST_MARKER" not in str(failure.value)


@pytest.mark.parametrize(
    ("path", "body", "sources", "expected"),
    [
        ("concepts/sample.md", "SECRET_TEST_MARKER", [], "two independent sources"),
        ("pending/sample.md", "SECRET_TEST_MARKER", [], "published status"),
        ("entities/sample.md", "<script>SECRET_TEST_MARKER</script>", [], "unsafe HTML"),
    ],
)
def test_page_validation_reports_rule_without_leaking_model_content(
    path: str, body: str, sources: list[str], expected: str,
) -> None:
    file = StructuredPage(path=path, title="Sample", body=body, sources=sources)
    with pytest.raises(ValidationError) as failure:
        file.to_wiki_page(
            None, source_revision_id="revision-1", now=datetime(2026, 9, 28, tzinfo=UTC),
        )
    explanation = str(_invalid_compilation_output(failure.value))
    assert expected in explanation
    assert "SECRET_TEST_MARKER" not in explanation


def test_compiler_reports_invalid_summary_without_storing_model_text() -> None:
    fake_model = FakeChatModel({
        "summary": "SECRET_TEST_MARKER" * 200,
        "files": [{
            "path": "sources/revision-1.md",
            "content": source_page("revision-1"),
        }],
        "conflicts": [],
    })
    compiler = WikiCompiler(cast(BaseChatModel, cast(Any, fake_model)))
    with pytest.raises(CompilationOutputError, match="invalid fields: summary") as failure:
        compiler.compile(
            source_revision_id="revision-1", source_markdown="text",
            schema="Create a source summary.", existing_pages={},
        )
    assert "SECRET_TEST_MARKER" not in str(failure.value)


def test_compiler_rejects_proposal_without_revision_summary() -> None:
    fake_model = FakeChatModel(
        {
            "summary": "Missing required summary",
            "files": [
                {
                    "path": "sources/another-revision.md",
                    "content": source_page("another-revision"),
                }
            ],
            "conflicts": [],
        }
    )
    compiler = WikiCompiler(cast(BaseChatModel, cast(Any, fake_model)))

    with pytest.raises(ValueError, match="sources/revision-1.md"):
        compiler.compile(
            source_revision_id="revision-1",
            source_markdown="Source content",
            schema="Create a source summary.",
            existing_pages={},
        )


def test_new_page_does_not_count_as_large_rewrite() -> None:
    assert _change_ratio(cast(Any, object()), "sources/new.md", "content", set()) == 0


class FakeWorkflow:
    def __init__(self) -> None:
        self.dispatched = False

    def apply_async(self) -> None:
        self.dispatched = True


def test_ingested_revision_queues_parse_and_compile_chain(monkeypatch: Any) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None)
    workflow = FakeWorkflow()
    monkeypatch.setattr(compilation, "chain", lambda *signatures: workflow)

    with Session(engine) as db:
        user = User(
            email="user@qq.com",
            display_name="User",
            password_hash="unused",
            role=UserRole.USER,
        )
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Manuals")
        db.add(wiki)
        db.flush()
        source = Source(
            wiki_id=wiki.id,
            name="manual.pdf",
            kind=SourceKind.PDF,
            created_by_id=user.id,
        )
        db.add(source)
        db.flush()
        revision = SourceRevision(
            source_id=source.id,
            sha256="a" * 64,
            object_path="sources/aa/object",
            media_type="application/pdf",
            size_bytes=100,
        )
        profile = ModelProfile(
            owner_id=user.id,
            name="Compiler",
            provider="custom",
            kind=ModelKind.CHAT,
            base_url="https://llm.example/v1",
            model_name="example",
            api_key_ciphertext="ciphertext",
        )
        db.add_all([revision, profile, FeatureFlag(key="auto_publish", enabled=True)])
        db.flush()
        db.add(
            ModelAssignment(
                user_id=user.id,
                purpose="compilation",
                model_profile_id=profile.id,
            )
        )
        db.commit()

        run = enqueue_ingested_revision(
            db,
            settings,
            source_revision_id=revision.id,
            created_by_id=user.id,
            wiki_id=wiki.id,
        )

        assert workflow.dispatched
        assert run.status == "queued"
        assert run.input_json["publication_policy"] == "low_risk_auto_publish"


def test_ingested_revision_is_retained_when_model_is_not_configured() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        user = User(
            email="user@qq.com",
            display_name="User",
            password_hash="unused",
            role=UserRole.USER,
        )
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Manuals")
        db.add(wiki)
        db.flush()
        source = Source(
            wiki_id=wiki.id,
            name="manual.pdf",
            kind=SourceKind.PDF,
            created_by_id=user.id,
        )
        db.add(source)
        db.flush()
        revision = SourceRevision(
            source_id=source.id,
            sha256="b" * 64,
            object_path="sources/bb/object",
            media_type="application/pdf",
            size_bytes=100,
        )
        db.add(revision)
        db.commit()

        run = enqueue_ingested_revision(
            db,
            Settings(_env_file=None),
            source_revision_id=revision.id,
            created_by_id=user.id,
            wiki_id=wiki.id,
        )

        assert db.get(SourceRevision, revision.id) is not None
        assert run.status == "failed"
        assert "No compilation model is assigned" in (run.error or "")


@pytest.mark.parametrize("index_fails", [False, True])
def test_compilation_stays_publishing_until_index_finishes(
    monkeypatch: Any, index_fails: bool
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    class FakeRepository:
        def __init__(self, root: object) -> None:
            pass

        def publish(self, *args: object, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(commit="b" * 40)

    class FakeGraph:
        def invoke(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {
                "files": {"sources/revision.md": "compiled"},
                "base_commit": "a" * 40,
                "status": "approved",
            }

    class FakeIndexer:
        def __init__(self, repository: object, wiki_id: str) -> None:
            pass

        def rebuild(self, db: Session, *args: object, **kwargs: object) -> None:
            assert db.get(AgentRun, run.id).status == "publishing"
            if index_fails:
                raise RuntimeError("index failed")

    class FakeFactory:
        def __init__(self, *args: object) -> None:
            pass

        def assigned_profile_id(self, purpose: str) -> None:
            return None

    monkeypatch.setattr(tasks, "open_wiki_repository", lambda *args: FakeRepository(None))
    monkeypatch.setattr(tasks, "_build_compilation_services", lambda *a, **k: object())
    def checkpointer_without_open_transaction(settings: Settings) -> nullcontext[None]:
        assert not db.in_transaction(), "Checkpoint setup must not wait on its own source read"
        return nullcontext(None)

    monkeypatch.setattr(tasks, "_compilation_checkpointer", checkpointer_without_open_transaction)
    monkeypatch.setattr(tasks, "build_compilation_graph", lambda *a: FakeGraph())
    monkeypatch.setattr(tasks, "WikiIndexer", FakeIndexer)
    monkeypatch.setattr(tasks, "ModelFactory", FakeFactory)

    with Session(engine) as db:
        user = User(email="writer@qq.com", display_name="Writer", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Workspace")
        db.add(wiki)
        db.flush()
        source = Source(
            wiki_id=wiki.id,
            name="manual.md",
            kind=SourceKind.MARKDOWN,
            created_by_id=user.id,
        )
        db.add(source)
        db.flush()
        revision = SourceRevision(
            source_id=source.id,
            sha256="a" * 64,
            object_path="sources/manual",
            media_type="text/markdown",
            size_bytes=12,
        )
        db.add(revision)
        db.flush()
        run = AgentRun(
            wiki_id=wiki.id,
            created_by_id=user.id,
            kind="compile_source",
            input_json={
                "source_revision_id": revision.id,
                "model_profile_id": "model",
                "publication_policy": "low_risk_auto_publish",
            },
        )
        db.add(run)
        db.commit()

        tasks._compile_source(db, run.id)

        db.refresh(run)
        assert run.status == ("published_index_failed" if index_fails else "completed")
        assert db.get(Draft, str(run.result_json["draft_id"])).published_commit == "b" * 40


def test_compilation_graph_releases_read_transactions_before_model_calls(
    monkeypatch: Any, tmp_path: Path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None)
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "AGENTS.md").write_text("Preserve citations.", encoding="utf-8")

    class FakeRepository:
        root = tmp_path

        def list_pages(self) -> list[str]:
            return []

        def head(self) -> str:
            return "a" * 40

    class FakeFactory:
        def __init__(self, db: Session, settings: Settings, user_id: str) -> None:
            self.db = db

        def chat(self, profile_id: str, *, timeout_seconds: int = 60) -> object:
            self.db.scalar(select(ModelProfile.id).limit(1))
            chat_timeouts.append(timeout_seconds)
            return object()

        def assigned_profile_id(self, purpose: str) -> None:
            self.db.scalar(select(ModelAssignment.model_profile_id).limit(1))
            return None

    class FakeCompiler:
        def __init__(self, model: object) -> None:
            pass

        def compile(self, **kwargs: object) -> SimpleNamespace:
            assert not db.in_transaction()
            return SimpleNamespace(
                files=[SimpleNamespace(path=f"sources/{revision.id}.md",
                                       content=source_page(revision.id))],
                summary="Compiled",
                conflicts=[],
            )

    class FakeVerifier:
        def __init__(self, model: object) -> None:
            pass

        def verify(self, files: object, schema: str) -> SimpleNamespace:
            assert not db.in_transaction()
            return SimpleNamespace(passed=True, issues=[])

    monkeypatch.setattr(tasks, "ModelFactory", FakeFactory)
    monkeypatch.setattr(tasks, "WikiCompiler", FakeCompiler)
    monkeypatch.setattr(tasks, "WikiVerifier", FakeVerifier)

    chat_timeouts: list[int] = []
    with Session(engine, expire_on_commit=False) as db:
        user = User(email="owner@qq.com", display_name="Owner", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Test Wiki")
        db.add(wiki)
        db.flush()
        source = Source(
            wiki_id=wiki.id, name="manual.md", kind=SourceKind.MARKDOWN,
            created_by_id=user.id,
        )
        db.add(source)
        db.flush()
        revision = SourceRevision(
            source_id=source.id, sha256="a" * 64, object_path="sources/manual",
            media_type="text/markdown", size_bytes=11, status="parsed",
        )
        db.add(revision)
        db.flush()
        db.add(SourceSpan(
            id=f"{revision.id}:0", source_revision_id=revision.id, ordinal=0,
            content="Model X-100 uses 12.5 V.", locator={},
        ))
        run = AgentRun(
            wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
            input_json={"source_revision_id": revision.id},
        )
        db.add(run)
        db.commit()

        services = tasks._build_compilation_services(
            db, run, settings, profile_id="model", repository=cast(Any, FakeRepository())
        )
        source_text = services.load_source(revision.id)
        assert "Model X-100" in source_text
        assert not db.in_transaction()
        compiled = services.compile_pages(source_text, revision.id)
        assert not db.in_transaction()
        services.verify(revision.id, compiled.files, [], "a" * 40)
        assert not db.in_transaction()
        assert chat_timeouts == [settings.compilation_model_timeout_seconds] * 2


def test_source_parsing_releases_transaction_before_embedding(
    monkeypatch: Any, tmp_path: Path
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, storage_root=tmp_path / "storage")

    class FakeParser:
        def parse(self, path: Path, kind: SourceKind) -> SimpleNamespace:
            assert not db.in_transaction()
            return SimpleNamespace(
                sections=(SimpleNamespace(
                    ordinal=0, heading=None, text="source text", locator={}
                ),),
                parser="text",
            )

    class CheckedEmbeddings:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            assert not db.in_transaction()
            return [[1.0, 0.0] for _ in texts]

    class FakeFactory:
        def __init__(self, db: Session, settings: Settings, user_id: str) -> None:
            self.db = db

        def assigned_profile_id(self, purpose: str) -> str:
            self.db.scalar(select(ModelAssignment.model_profile_id).limit(1))
            return "embedding-profile"

        def embeddings(self, profile_id: str) -> CheckedEmbeddings:
            self.db.scalar(select(ModelProfile.id).limit(1))
            return CheckedEmbeddings()

    monkeypatch.setattr(tasks, "DocumentParser", FakeParser)
    monkeypatch.setattr(tasks, "ModelFactory", FakeFactory)
    with Session(engine, expire_on_commit=False) as db:
        user = User(email="parser@qq.com", display_name="Parser", password_hash="unused")
        db.add(user)
        db.flush()
        wiki = PersonalWiki(owner_id=user.id, name="Parser Wiki")
        db.add(wiki)
        db.flush()
        source = Source(
            wiki_id=wiki.id, name="source.txt", kind=SourceKind.TEXT, created_by_id=user.id
        )
        db.add(source)
        db.flush()
        revision = SourceRevision(
            source_id=source.id, sha256="a" * 64,
            object_path=f"sources/aa/{'a' * 64}", media_type="text/plain", size_bytes=11,
        )
        db.add(revision)
        db.flush()
        run = AgentRun(
            wiki_id=wiki.id, created_by_id=user.id, kind="compile_source",
            input_json={"source_revision_id": revision.id},
        )
        db.add(run)
        db.commit()

        db.scalar(select(Source.id).where(Source.id == source.id))
        tasks._parse_revision(db, run, revision, source, settings)
        assert revision.status == "parsed"
        assert db.get(SourceSpan, f"{revision.id}:0") is not None


@pytest.mark.parametrize("setup_fails", [False, True])
def test_checkpoint_migrations_release_advisory_lock(
    monkeypatch: Any, setup_fails: bool
) -> None:
    statements: list[str] = []

    class FakeConnection:
        def cursor(self) -> nullcontext[SimpleNamespace]:
            return nullcontext(SimpleNamespace(execute=statements.append))

    class FakeSaver:
        conn = FakeConnection()

        def setup(self) -> None:
            statements.append("setup")
            if setup_fails:
                raise RuntimeError("migration failed")

    @contextmanager
    def fake_connection(connection_string: str) -> Any:
        yield FakeSaver()

    monkeypatch.setattr(tasks.PostgresSaver, "from_conn_string", fake_connection)
    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://wiki:wiki@localhost/wiki"
    )
    if setup_fails:
        with pytest.raises(RuntimeError, match="migration failed"), tasks._compilation_checkpointer(
            settings
        ):
            pass
    else:
        with tasks._compilation_checkpointer(settings):
            statements.append("graph")

    assert statements[0] == "SET lock_timeout = '30s'"
    assert "pg_advisory_lock(" in statements[1]
    assert statements[2] == "setup"
    assert "pg_advisory_unlock(" in statements[3]
    assert statements[4] == "RESET lock_timeout"
    if not setup_fails:
        assert statements[5] == "graph"
