from io import BytesIO
from pathlib import Path

import pytest
from wiki_agent.sources.storage import SourceObjectStore, SourceTooLargeError


def test_object_store_is_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    store = SourceObjectStore(tmp_path, max_bytes=100)
    first = store.put(BytesIO(b"same content"))
    second = store.put(BytesIO(b"same content"))
    assert first.sha256 == second.sha256
    assert first.path == second.path
    assert first.path.read_bytes() == b"same content"


def test_object_store_rejects_large_files(tmp_path: Path) -> None:
    store = SourceObjectStore(tmp_path, max_bytes=3)
    with pytest.raises(SourceTooLargeError):
        store.put(BytesIO(b"four"))

