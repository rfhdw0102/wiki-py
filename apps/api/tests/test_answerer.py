from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from wiki_agent.llm.answerer import WikiAnswerer
from wiki_agent.search.fusion import FusedHit


def test_answerer_reports_insufficient_evidence_without_calling_model() -> None:
    model = FakeMessagesListChatModel(responses=[AIMessage(content="unused")])
    result = WikiAnswerer(model).answer("Unknown?", [], layer="wiki")
    assert result.insufficient_evidence is True
    assert result.citations == []


def test_evidence_renderer_rejects_too_small_context() -> None:
    model = FakeMessagesListChatModel(responses=[AIMessage(content="unused")])
    hit = FusedHit(
        document_id="sources/manual.md",
        score=1.0,
        channels=("bm25",),
        content="Long evidence",
        metadata={},
    )
    try:
        WikiAnswerer(model, max_evidence_characters=1).answer("Question?", [hit], layer="wiki")
    except ValueError as exc:
        assert "context limit" in str(exc)
    else:
        raise AssertionError("oversized evidence must fail explicitly")
