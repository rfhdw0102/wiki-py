import json
from urllib.parse import urlparse

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel


def uses_deepseek_json_mode(model: BaseChatModel) -> bool:
    return (
        isinstance(model, ChatOpenAI)
        and urlparse(str(model.openai_api_base)).hostname == "api.deepseek.com"
    )


def invoke_structured(
    model: BaseChatModel,
    schema: type[BaseModel],
    messages: list[BaseMessage],
    *,
    raw_json: bool = False,
) -> BaseModel | dict[str, object]:
    if uses_deepseek_json_mode(model):
        instructions = SystemMessage(
            content=(
                "Return only one JSON object matching this JSON schema, with no Markdown: "
                f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
            )
        )
        output_schema = schema.model_json_schema() if raw_json else schema
        return model.with_structured_output(output_schema, method="json_mode").invoke(
            [instructions, *messages]
        )
    return model.with_structured_output(schema, method="function_calling").invoke(messages)
