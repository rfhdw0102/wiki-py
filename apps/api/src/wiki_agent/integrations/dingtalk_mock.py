"""Offline fixtures only: no DingTalk SDK, credentials, or network transport."""

from dataclasses import dataclass

FIXTURE_VERSION = "fixture-v1"
NOTICE = "模拟钉钉，未连接真实钉钉"
SOURCE_NOTE = "来源：模拟钉钉（未连接真实钉钉）。 [^sim-1] [^sim-2]"
RUN_KIND = "dingtalk_mock_import"


@dataclass(frozen=True)
class MockDocument:
    document_id: str
    title: str
    canonical_text: str


_DOCUMENTS = (
    MockDocument(
        "mock-a",
        "模拟文档A：项目说明",
        "这是模拟文档 A 的第一段，说明本地导入流程。English: Alpha only.\n\n"
        "这是模拟文档 A 的第二段，保留中文、English 和标点；顺序不得改变。",
    ),
    MockDocument(
        "mock-b",
        "模拟文档B：会议记录",
        "这是模拟文档 B 的第一段，B_ONLY 内容不应出现在 A 的页面。\n\n"
        "这是模拟文档 B 的第二段，English: Beta only.",
    ),
)


def list_documents() -> list[dict[str, str]]:
    return [{"document_id": item.document_id, "title": item.title} for item in _DOCUMENTS]


def get_document(document_id: str) -> MockDocument:
    for item in _DOCUMENTS:
        if item.document_id == document_id:
            return item
    raise KeyError(document_id)
