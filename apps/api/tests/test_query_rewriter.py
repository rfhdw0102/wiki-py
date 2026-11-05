from wiki_agent.llm.query_rewriter import QueryPlan, WikiQueryRewriter


class StructuredModel:
    def invoke(self, messages: object) -> dict[str, str]:
        return {
            "lexical_query": "A-103 轴承 温度",
            "semantic_query": "A-103 设备轴承温度异常的处理方法",
        }


class ChatModel:
    def with_structured_output(
        self, schema: object, *, method: str
    ) -> StructuredModel:
        assert schema is QueryPlan
        assert method == "function_calling"
        return StructuredModel()


def test_query_rewriter_returns_separate_lexical_and_semantic_queries() -> None:
    result = WikiQueryRewriter(ChatModel()).rewrite("三号设备转动的地方很烫怎么办")

    assert result.lexical_query == "A-103 轴承 温度"
    assert result.semantic_query == "A-103 设备轴承温度异常的处理方法"
