import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx


class UnsafeSourceUrlError(ValueError):
    pass


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    content_type: str
    body: bytes


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeSourceUrlError("only absolute HTTP(S) URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeSourceUrlError("URL credentials are not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeSourceUrlError("source hostname cannot be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise UnsafeSourceUrlError(f"source resolves to a non-public address: {ip}")


def fetch_page(
    url: str,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    max_redirects: int = 5,
    timeout_seconds: float = 15,
) -> FetchedPage:
    current = url
    requested = url
    with httpx.Client(
        follow_redirects=False,
        timeout=timeout_seconds,
        trust_env=False,
    ) as client:
        for _ in range(max_redirects + 1):
            validate_public_url(current)
            with client.stream(
                "GET",
                current,
                headers={"User-Agent": "LLMWikiAgent/0.1 (+self-hosted knowledge compiler)"},
            ) as response:
                _validate_peer(response)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise httpx.HTTPError("redirect response has no Location header")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                    raise httpx.HTTPError(f"unsupported web content type: {content_type}")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise httpx.HTTPError(f"web response exceeds {max_bytes} bytes")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise httpx.HTTPError(f"web response exceeds {max_bytes} bytes")
                return FetchedPage(requested, str(response.url), content_type, bytes(body))
    raise httpx.TooManyRedirects(f"source exceeded {max_redirects} redirects")


def _validate_peer(response: httpx.Response) -> None:
    stream = response.extensions.get("network_stream")
    getter = getattr(stream, "get_extra_info", None)
    if not callable(getter):
        raise UnsafeSourceUrlError("HTTP transport did not expose the connected peer address")
    peer = getter("server_addr")
    if not isinstance(peer, tuple) or not peer:
        raise UnsafeSourceUrlError("HTTP transport returned an invalid peer address")
    ip = ipaddress.ip_address(str(peer[0]))
    if not ip.is_global:
        raise UnsafeSourceUrlError(f"HTTP connection reached a non-public address: {ip}")
