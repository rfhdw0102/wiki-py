import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import DateTime, create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from wiki_agent.config import get_settings


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(
    dbapi_connection: object,
    _connection_record: object,
) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def ensure_question_checkpoint_schema(bind: Engine) -> None:
    columns = inspect(bind).get_columns("question_runs")
    if not any(column["name"] == "checkpoint_json" for column in columns):
        with bind.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE question_runs "
                    "ADD COLUMN checkpoint_json JSON NOT NULL DEFAULT '{}'"
                )
            )


def ensure_conversation_title_schema(bind: Engine) -> None:
    columns = inspect(bind).get_columns("conversations")
    if not any(column["name"] == "title_is_custom" for column in columns):
        with bind.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE conversations "
                    "ADD COLUMN title_is_custom BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
