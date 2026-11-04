import json

import httpx
import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from wiki_agent.llm.query_rewriter import QueryPlan
from wiki_agent.llm.structured import invoke_structured


def test_deepseek_thinking_uses_json_mode_without_forced_tool_choice() -> None:
    requests: list[dict[str, object]] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"lexical_query":"safety","semantic_query":"safety rules"}',
                        },
                    }
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(reply))
    model = ChatOpenAI(
        model="deepseek-flash",
        api_key="test-only",
        base_url="https://api.deepseek.com",
        http_client=client,
    )
    result = invoke_structured(
        model, QueryPlan, [HumanMessage(content="Summarize safety rules")]
    )

    assert result == QueryPlan(
        lexical_query="safety", semantic_query="safety rules"
    )
    assert len(requests) == 1
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert "tools" not in requests[0]
    assert "tool_choice" not in requests[0]
    assert "json" in json.dumps(requests[0]["messages"][0]).lower()
    assert "lexical_query" in json.dumps(requests[0]["messages"][0])


def test_json_mode_still_validates_the_response() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"lexical_query":"x"}'},
                    }
                ],
            },
        )

    model = ChatOpenAI(
        model="deepseek-flash",
        api_key="test-only",
        base_url="https://api.deepseek.com",
        http_client=httpx.Client(transport=httpx.MockTransport(reply)),
    )
    with pytest.raises(OutputParserException, match="semantic_query"):
        invoke_structured(model, QueryPlan, [HumanMessage(content="test")])
