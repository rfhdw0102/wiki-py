import httpx
import pytest
from wiki_agent.sources.fetcher import UnsafeSourceUrlError, _validate_peer, validate_public_url


class Stream:
    def __init__(self, address: str) -> None:
        self.address = address

    def get_extra_info(self, key: str) -> tuple[str, int] | None:
        return (self.address, 443) if key == "server_addr" else None


def test_public_url_rejects_loopback() -> None:
    with pytest.raises(UnsafeSourceUrlError, match="non-public"):
        validate_public_url("http://127.0.0.1/internal")


def test_connected_peer_is_revalidated() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.com"),
        extensions={"network_stream": Stream("10.0.0.1")},
    )
    with pytest.raises(UnsafeSourceUrlError, match="connection reached"):
        _validate_peer(response)
