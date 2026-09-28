from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.api.knowledge_gaps import list_knowledge_gaps
from wiki_agent.db import Base
from wiki_agent.models import QueryLog, User, UserRole


def test_knowledge_gaps_only_include_fallback_or_empty_queries() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as db:
        admin = User(
            email="admin@example.com",
            display_name="Admin",
            password_hash="not-used",
            role=UserRole.ADMIN,
        )
        db.add(admin)
        db.flush()
        db.add_all(
            [
                QueryLog(
                    user_id=admin.id,
                    query="missing policy",
                    layer="raw",
                    fallback_used=True,
                    hit_count=2,
                    channels=["raw_bm25"],
                    created_at=now - timedelta(minutes=2),
                ),
                QueryLog(
                    user_id=admin.id,
                    query="missing policy",
                    layer="raw",
                    fallback_used=True,
                    hit_count=4,
                    channels=["raw_bm25"],
                    created_at=now,
                ),
                QueryLog(
                    user_id=admin.id,
                    query="no evidence",
                    layer="wiki",
                    fallback_used=False,
                    hit_count=0,
                    channels=["bm25", "graph"],
                    created_at=now - timedelta(minutes=1),
                ),
                QueryLog(
                    user_id=admin.id,
                    query="covered topic",
                    layer="wiki",
                    fallback_used=False,
                    hit_count=3,
                    channels=["bm25", "graph"],
                    created_at=now,
                ),
            ]
        )
        db.commit()

        gaps = list_knowledge_gaps(admin, db)

    assert [gap["query"] for gap in gaps] == ["missing policy", "no evidence"]
    assert gaps[0]["occurrences"] == 2
    assert gaps[0]["best_hit_count"] == 4
