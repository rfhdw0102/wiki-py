import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from wiki_agent.db import Base
from wiki_agent.models import Conversation, ConversationMessage, PersonalWiki, User, UserRole
from wiki_agent.qa.memory import (
    MemoryCompressionError,
    MemorySummary,
    prepare_history,
    tool_context,
)


class SummaryModel:
    def __init__(self, response: str = "用户正在询问 3 号空压机的维修时间。") -> None:
        self.response = response
        self.chunks: list[str] = []

    def with_structured_output(self, schema: type, *, method: str) -> "SummaryModel":
        assert schema is MemorySummary
        assert method == "function_calling"
        return self

    def invoke(self, messages: list[object]) -> dict[str, str]:
        self.chunks.append(str(messages[-1].content))
        return {"topic_memory": self.response}


def create_conversation(db: Session) -> Conversation:
    user = User(
        email="owner@qq.com",
        display_name="Owner",
        password_hash="unused",
        role=UserRole.USER,
    )
    db.add(user)
    db.flush()
    wiki = PersonalWiki(owner_id=user.id, name="Manuals")
    db.add(wiki)
    db.flush()
    conversation = Conversation(user_id=user.id, wiki_id=wiki.id, title="Maintenance")
    db.add(conversation)
    db.flush()
    return conversation


def test_oversized_recent_message_is_compacted_without_changing_original() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        conversation = create_conversation(db)
        content = "3号空压机的维修记录。" * 500
        message = ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=content,
        )
        db.add(message)
        db.commit()
        model = SummaryModel()
        question = "它上次什么时候维修的？"

        prepared = prepare_history(
            db,
            conversation,
            model,
            question,
            byte_budget=6144,
        )
        db.commit()
        db.refresh(message)
        db.refresh(conversation)

        assert message.content == content
        assert conversation.memory_cursor_id == message.id
        assert prepared.messages == []
        assert prepared.topic_memory == model.response
        assert len(model.chunks) > 1
        assert question not in "".join(model.chunks)


def test_recent_messages_remain_verbatim_while_older_messages_are_compacted() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        conversation = create_conversation(db)
        messages = [
            ConversationMessage(
                conversation_id=conversation.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"第 {index} 条",
            )
            for index in range(8)
        ]
        db.add_all(messages)
        db.commit()
        model = SummaryModel()

        prepared = prepare_history(
            db,
            conversation,
            model,
            "最近怎么样？",
            byte_budget=6144,
        )
        db.commit()

        assert prepared.messages == [(item.role, item.content) for item in messages[-6:]]
        assert conversation.memory_cursor_id == messages[1].id
        assert prepared.topic_memory == model.response
        assert len(model.chunks) == 1

        repeated = prepare_history(
            db,
            conversation,
            model,
            "最近怎么样？",
            byte_budget=6144,
        )
        assert repeated.messages == prepared.messages
        assert len(model.chunks) == 1


def test_invalid_compression_does_not_consume_history() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        conversation = create_conversation(db)
        message = ConversationMessage(
            conversation_id=conversation.id,
            role="assistant",
            content="长消息。" * 2000,
        )
        db.add(message)
        db.commit()

        with pytest.raises(MemoryCompressionError, match="invalid data"):
            prepare_history(
                db,
                conversation,
                SummaryModel("太长" * 700),
                "当前问题",
                byte_budget=6144,
            )
        db.rollback()
        db.refresh(conversation)
        db.refresh(message)
        assert conversation.memory_cursor_id is None
        assert message.content == "长消息。" * 2000


def test_tool_context_keeps_question_and_drops_older_raw_history_under_budget() -> None:
    prompt = tool_context(
        "设备故障怎么办？",
        "3号空压机故障怎么办？",
        [("assistant", "很长的旧回答。" * 1000), ("user", "3号空压机")],
        byte_budget=6144,
    )
    assert len(prompt.encode("utf-8")) <= 6144 - 512
    assert "Current question: 设备故障怎么办？" in prompt
    assert "Standalone retrieval question: 3号空压机故障怎么办？" in prompt
    assert "很长的旧回答。" not in prompt


def test_many_unsummarized_messages_compact_in_batches() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        conversation = create_conversation(db)
        messages = [
            ConversationMessage(
                conversation_id=conversation.id,
                role="user" if index % 2 == 0 else "assistant",
                content=f"关于设备的第 {index} 条消息",
            )
            for index in range(46)
        ]
        db.add_all(messages)
        db.commit()
        model = SummaryModel()
        prepared = prepare_history(
            db,
            conversation,
            model,
            "它怎么样？",
            byte_budget=6144,
        )
        assert prepared.messages == [(item.role, item.content) for item in messages[-6:]]
        assert conversation.memory_cursor_id == messages[39].id
        assert model.chunks


def test_interrupted_compression_reuses_completed_chunks_without_losing_history() -> None:
    class InterruptedSummary(SummaryModel):
        def invoke(self, messages: list[object]) -> dict[str, str]:
            if len(self.chunks) == 1:
                raise ConnectionError("summary provider disconnected")
            return super().invoke(messages)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        conversation = create_conversation(db)
        message = ConversationMessage(
            conversation_id=conversation.id, role="assistant", content="维修记录。" * 1200,
        )
        db.add(message)
        db.commit()
        cache: dict[str, str] = {}
        with pytest.raises(ConnectionError):
            prepare_history(
                db, conversation, InterruptedSummary(), "它怎么样？",
                byte_budget=6144, summary_cache=cache,
                before_model=db.commit, on_summary=db.commit,
            )
        db.rollback()
        db.refresh(conversation)
        assert len(cache) == 1
        assert conversation.memory_cursor_id is None
        model = SummaryModel()
        prepared = prepare_history(
            db, conversation, model, "它怎么样？",
            byte_budget=6144, summary_cache=cache,
            before_model=db.commit, on_summary=db.commit,
        )
        assert len(model.chunks) == len(cache) - 1
        assert prepared.topic_memory == model.response
        assert conversation.memory_cursor_id == message.id
        assert message.content == "维修记录。" * 1200
