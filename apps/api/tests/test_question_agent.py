import pytest
from langchain_core.messages import AIMessage, ToolMessage
from wiki_agent.llm.answerer import AnswerResult
from wiki_agent.qa.agent import (
    MAX_MODEL_STEPS,
    MAX_TOOL_CALLS,
    AgentCheckpoint,
    ConversationQuestionPlan,
    KnowledgeQuestionAgent,
    QuestionAgentError,
)
from wiki_agent.search.fusion import FusedHit


class StructuredResponse:
    def __init__(self, schema: type) -> None:
        self.schema = schema

    def invoke(self, messages: object) -> object:
        if self.schema is ConversationQuestionPlan:
            return {
                "standalone_question": "When was machine A repaired?",
                "needs_clarification": False,
            }
        assert self.schema is AnswerResult
        return {"answer": "It was repaired on Tuesday.", "citations": ["wiki/a.md"]}


class ToolCallingModel:
    def __init__(self) -> None:
        self.turn = 0

    def with_structured_output(
        self, schema: type, *, method: str
    ) -> StructuredResponse:
        assert method == "function_calling"
        return StructuredResponse(schema)

    def bind_tools(self, schemas: list[type]) -> "ToolCallingModel":
        assert {schema.__name__ for schema in schemas} == {
            "SearchWiki",
            "ReadWikiPage",
            "ExpandGraph",
            "SearchRaw",
            "ReadSourceSpan",
        }
        return self

    def invoke(self, messages: object) -> AIMessage:
        self.turn += 1
        if self.turn == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "SearchWiki",
                        "args": {"query": "machine A repair"},
                    }
                ],
            )
        return AIMessage(content="Enough evidence.")


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._hits: list[FusedHit] = []
        self.factory = self
        self.reranker_profile_id: str | None = None

    def assigned_profile_id(self, purpose: str) -> None:
        return None

    def call(self, name: str, args: dict[str, object]) -> dict[str, object]:
        self.calls.append(name)
        self._hits = [FusedHit("wiki/a.md", 1.0, ("bm25",), "Repair on Tuesday", {"layer": "wiki"})]
        return {"results": [{"document_id": "wiki/a.md", "content": "Repair on Tuesday"}]}

    def final_evidence(self) -> list[FusedHit]:
        return self._hits

    def snapshot_evidence(self) -> list[FusedHit]:
        return self._hits

    def restore_evidence(self, evidence: list[FusedHit]) -> None:
        self._hits = evidence


def test_agent_calls_tools_before_grounded_answer() -> None:
    model = ToolCallingModel()
    tools = FakeTools()
    agent = KnowledgeQuestionAgent(model, tools)
    plan = agent.resolve("When was it repaired?", [("user", "machine A")])
    updates: list[str] = []

    result = agent.answer(
        "When was it repaired?",
        plan,
        [("user", "machine A")],
        on_progress=lambda stage, trace, evidence: updates.append(stage),
    )

    assert plan.standalone_question == "When was machine A repaired?"
    assert tools.calls == ["SearchWiki"]
    assert result.answer.citations == ["wiki/a.md"]
    assert [entry["tool"] for entry in result.trace] == ["SearchWiki"]
    assert updates == ["retrieving", "answering"]


def test_ambiguous_question_asks_for_clarification_without_tools() -> None:
    model = ToolCallingModel()
    tools = FakeTools()
    outcome = KnowledgeQuestionAgent(model, tools).answer(
        "When was it repaired?",
        ConversationQuestionPlan(
            standalone_question="When was it repaired?",
            needs_clarification=True,
            clarification_question="Which machine do you mean?",
        ),
        [],
    )

    assert outcome.clarification_question == "Which machine do you mean?"
    assert outcome.answer.insufficient_evidence is True
    assert tools.calls == []
    assert model.turn == 0


def test_agent_rejects_model_without_tool_calling() -> None:
    class NoTools(ToolCallingModel):
        def bind_tools(self, schemas: list[type]) -> None:
            raise NotImplementedError

    with pytest.raises(QuestionAgentError, match="does not support tool calling"):
        KnowledgeQuestionAgent(NoTools(), FakeTools()).answer(
            "Question",
            ConversationQuestionPlan(standalone_question="Question"),
            [],
        )


def test_agent_enforces_tool_call_budget() -> None:
    class TooManyCalls(ToolCallingModel):
        def invoke(self, messages: object) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"call-{index}",
                        "name": "SearchWiki",
                        "args": {"query": f"search {index}"},
                    }
                    for index in range(MAX_TOOL_CALLS + 1)
                ],
            )

    with pytest.raises(QuestionAgentError, match="budget exceeded"):
        KnowledgeQuestionAgent(TooManyCalls(), FakeTools()).answer(
            "Question",
            ConversationQuestionPlan(standalone_question="Question"),
            [],
        )


def test_agent_rejects_unretrieved_answer() -> None:
    class NoSearch(ToolCallingModel):
        def invoke(self, messages: object) -> AIMessage:
            return AIMessage(content="I already know the answer.")

    with pytest.raises(QuestionAgentError, match="did not request knowledge retrieval"):
        KnowledgeQuestionAgent(NoSearch(), FakeTools()).answer(
            "Question",
            ConversationQuestionPlan(standalone_question="Question"),
            [],
        )


def test_resume_continues_pending_tool_batch_without_repeating_completed_tool() -> None:
    class TwoCalls(ToolCallingModel):
        def invoke(self, messages: object) -> AIMessage:
            self.turn += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {"id": "first", "name": "SearchWiki", "args": {"query": "machine A"}},
                    {"id": "second", "name": "ReadWikiPage", "args": {"page_id": "wiki/a.md"}},
                ],
            )

    class InterruptedTools(FakeTools):
        def call(self, name: str, args: dict[str, object]) -> dict[str, object]:
            if name == "ReadWikiPage":
                raise ConnectionError("connection dropped during second tool")
            return super().call(name, args)

    model = TwoCalls()
    tools = InterruptedTools()
    saved: list[dict[str, object]] = []
    plan = ConversationQuestionPlan(standalone_question="machine A")
    with pytest.raises(ConnectionError):
        KnowledgeQuestionAgent(model, tools).answer(
            "machine A", plan, [],
            on_checkpoint=lambda state: saved.append(state.model_dump(mode="json")),
        )
    state = AgentCheckpoint.model_validate(saved[-1])
    assert state.phase == "tools"
    assert state.next_tool == 1
    assert state.model_steps == 1
    assert tools.calls == ["SearchWiki"]

    class ContinueModel(ToolCallingModel):
        def invoke(self, messages: list[object]) -> AIMessage:
            self.turn += 1
            assert [m.tool_call_id for m in messages if isinstance(m, ToolMessage)] == [
                "first", "second"
            ]
            return AIMessage(content="Enough evidence.")

    restored_tools = FakeTools()
    resumed_model = ContinueModel()
    result = KnowledgeQuestionAgent(resumed_model, restored_tools).answer(
        "machine A", plan, [], checkpoint=state,
    )
    assert restored_tools.calls == ["ReadWikiPage"]
    assert resumed_model.turn == 1
    assert [entry["tool"] for entry in result.trace] == ["SearchWiki", "ReadWikiPage"]
    assert result.answer.citations == ["wiki/a.md"]


def test_resume_after_planning_failure_preserves_messages_and_evidence() -> None:
    class BrokenPlanning(ToolCallingModel):
        def invoke(self, messages: object) -> AIMessage:
            if self.turn == 1:
                raise ConnectionError("provider disconnected")
            return super().invoke(messages)

    saved: list[dict[str, object]] = []
    plan = ConversationQuestionPlan(standalone_question="machine A")
    with pytest.raises(ConnectionError):
        KnowledgeQuestionAgent(BrokenPlanning(), FakeTools()).answer(
            "machine A", plan, [],
            on_checkpoint=lambda state: saved.append(state.model_dump(mode="json")),
        )
    checkpoint = AgentCheckpoint.model_validate(saved[-1])
    assert checkpoint.phase == "planning"
    assert checkpoint.model_steps == 1
    assert checkpoint.evidence[0].content == "Repair on Tuesday"
    model = ToolCallingModel()
    model.turn = 1
    tools = FakeTools()
    result = KnowledgeQuestionAgent(model, tools).answer(
        "machine A", plan, [], checkpoint=checkpoint,
    )
    assert tools.calls == []
    assert result.answer.citations == ["wiki/a.md"]
    assert len(result.trace) == 1


def test_completed_checkpoint_returns_saved_answer_without_model_or_tool_calls() -> None:
    saved: list[dict[str, object]] = []
    plan = ConversationQuestionPlan(standalone_question="machine A")
    first = KnowledgeQuestionAgent(ToolCallingModel(), FakeTools()).answer(
        "machine A", plan, [],
        on_checkpoint=lambda state: saved.append(state.model_dump(mode="json")),
    )

    class NoModelCalls(ToolCallingModel):
        def bind_tools(self, schemas: list[type]) -> "ToolCallingModel":
            raise AssertionError("tools must not be rebound")

        def with_structured_output(self, schema: type, *, method: str) -> StructuredResponse:
            raise AssertionError("the saved answer must be reused")

    tools = FakeTools()
    result = KnowledgeQuestionAgent(NoModelCalls(), tools).answer(
        "machine A", plan, [], checkpoint=AgentCheckpoint.model_validate(saved[-1]),
    )
    assert result == first
    assert tools.calls == []


def test_resume_does_not_reset_the_model_step_budget() -> None:
    checkpoint = AgentCheckpoint(model_steps=MAX_MODEL_STEPS)
    model = ToolCallingModel()
    with pytest.raises(QuestionAgentError, match="reasoning step budget"):
        KnowledgeQuestionAgent(model, FakeTools()).answer(
            "machine A", ConversationQuestionPlan(standalone_question="machine A"), [],
            checkpoint=checkpoint,
        )
    assert model.turn == 0
