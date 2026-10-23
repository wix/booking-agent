"""Hosts that must use an HTTP proxy, including bracketed IPv6 in NO_PROXY."""

from __future__ import annotations

import httpx
import pytest

# Bracketed IPv6 is a duplicate of the bare address. httpx accepts the bare
# form and rejects the brackets as an invalid port.
_NO_PROXY = (
    "localhost,127.0.0.1,::1,[::1],198.19.0.1,198.19.0.2,"
    "fd8b:4f84:7d32:99::1,[fd8b:4f84:7d32:99::1],"
    "fd8b:4f84:7d32:99::2,[fd8b:4f84:7d32:99::2]"
)


def _proxy_env(monkeypatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", _NO_PROXY)
    monkeypatch.setenv("no_proxy", _NO_PROXY)


def _proxy_host(transport: object) -> str | None:
    pool = getattr(transport, "_pool", None)
    url = getattr(pool, "_proxy_url", None)
    if url is None:
        return None
    host = url.host
    return host.decode() if isinstance(host, bytes) else host


def test_plain_client_accepts_bracketed_ipv6_no_proxy(monkeypatch):
    """Prerequisite checks use a plain httpx client and must build under this NO_PROXY."""
    _proxy_env(monkeypatch)
    from booking_agent.http_trace import tolerate_proxy_env

    tolerate_proxy_env()
    with httpx.Client() as client:
        patterns = [pattern.pattern for pattern in client._mounts]
    assert "all://::1" in patterns or "all://[::1]" in patterns
    assert "all://localhost" in patterns
    assert not any("*[" in pattern for pattern in patterns)


@pytest.mark.asyncio
async def test_traced_client_sends_https_through_the_env_proxy(monkeypatch):
    _proxy_env(monkeypatch)
    from booking_agent.http_trace import _CircuitTransport, traced_async_client

    async with traced_async_client() as client:
        mounted = next(
            transport
            for pattern, transport in client._mounts.items()
            if pattern.pattern == "https://"
        )
        assert isinstance(mounted, _CircuitTransport)
        assert _proxy_host(mounted._inner) == "proxy.example"
        direct = [
            pattern.pattern
            for pattern, transport in client._mounts.items()
            if transport is None
        ]
        assert "all://localhost" in direct
        assert "all://127.0.0.1" in direct
        assert isinstance(client._transport, _CircuitTransport)


@pytest.mark.asyncio
async def test_explicit_transport_stays_on_that_transport(monkeypatch):
    _proxy_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    from booking_agent.http_trace import traced_async_client

    transport = httpx.MockTransport(handler)
    async with traced_async_client(transport=transport) as client:
        response = await client.get("https://registry.example/search")
    assert response.status_code == 204
    assert response.request.url.host == "registry.example"
