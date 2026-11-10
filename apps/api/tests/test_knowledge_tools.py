import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.config import Settings
from wiki_agent.db import Base
from wiki_agent.models import (
    FeatureFlag,
    PersonalWiki,
    Source,
    SourceKind,
    SourceRevision,
    SourceSpan,
    User,
    UserRole,
    WikiDocument,
)
from wiki_agent.qa.tools import KnowledgeToolError, KnowledgeTools


def test_tools_never_read_other_wikis_and_do_not_offer_filesystem_access() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(
            email="owner@qq.com",
            display_name="Owner",
            password_hash="unused",
            role=UserRole.USER,
        )
        db.add(user)
        db.flush()
        first = PersonalWiki(owner_id=user.id, name="First")
        other = PersonalWiki(owner_id=user.id, name="Other")
        db.add_all([first, other, FeatureFlag(key="raw_rag", enabled=True)])
        db.flush()
        db.add(
            WikiDocument(
                wiki_id=other.id,
                path="concepts/secret.md",
                page_id="secret",
                page_type="concept",
                title="Secret",
                content="Other Wiki secret",
                commit_sha="a" * 40,
            )
        )
        source = Source(
            wiki_id=other.id,
            name="secret.txt",
            kind=SourceKind.TEXT,
            created_by_id=user.id,
        )
        db.add(source)
        db.flush()
        revision = SourceRevision(
            source_id=source.id,
            sha256="a" * 64,
            object_path="sources/aa/object",
            media_type="text/plain",
            size_bytes=20,
        )
        db.add(revision)
        db.flush()
        db.add(
            SourceSpan(
                id=f"{revision.id}:0",
                source_revision_id=revision.id,
                ordinal=0,
                content="Other Wiki secret",
                locator={},
            )
        )
        db.commit()

        tools = KnowledgeTools(db, Settings(_env_file=None), user.id, first.id)
        assert tools.call("SearchWiki", {"query": "secret"}) == {"results": []}
        assert tools.call("SearchRaw", {"query": "secret"}) == {"results": []}
        with pytest.raises(KnowledgeToolError, match="current Wiki"):
            tools.call("ReadWikiPage", {"page_id": "concepts/secret.md"})
        with pytest.raises(KnowledgeToolError, match="current Wiki"):
            tools.call("ReadSourceSpan", {"span_id": f"raw:{revision.id}:0"})
        with pytest.raises(KnowledgeToolError, match="Unsupported"):
            tools.call("ReadFile", {"path": "/etc/passwd"})


def test_raw_tools_respect_feature_flag() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        tools = KnowledgeTools(db, Settings(_env_file=None), "user", "wiki")
        with pytest.raises(KnowledgeToolError, match="disabled"):
            tools.call("SearchRaw", {"query": "private"})
        with pytest.raises(KnowledgeToolError, match="disabled"):
            tools.call("ReadSourceSpan", {"span_id": "raw:fake"})
        db.add(FeatureFlag(key="raw_rag", enabled=True))
        db.commit()
        explicitly_disabled = KnowledgeTools(
            db,
            Settings(_env_file=None),
            "user",
            "wiki",
            allow_raw=False,
        )
        with pytest.raises(KnowledgeToolError, match="disabled"):
            explicitly_disabled.call("SearchRaw", {"query": "private"})
