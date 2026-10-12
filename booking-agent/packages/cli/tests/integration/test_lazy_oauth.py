from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from booking_agent.backend.live import LiveUSPBackend
from booking_agent.discovery import (
    USPBusinessInfo,
    USPBusinessProfile,
    USPTransportBinding,
)
from booking_agent.rest_client import USPBusinessRestClient, USPTokenStore


def _profile() -> USPBusinessProfile:
    return USPBusinessProfile(
        version="1.0",
        services={
            "dev.usp-protocol.services": [
                USPTransportBinding(
                    version="1.0",
                    transport="rest",
                    endpoint="https://biz.example.com/usp/v1",
                )
            ]
        },
        capabilities={},
        checkout_systems=[],
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )


@pytest.mark.asyncio
async def test_oauth_metadata_failure_marks_endpoint_unavailable(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400)

    transport = httpx.MockTransport(handler)

    def fake_traced(**kwargs):
        merged = dict(kwargs)
        merged.setdefault("follow_redirects", True)
        return httpx.AsyncClient(transport=transport, **merged)

    monkeypatch.setattr("booking_agent.rest_client.traced_async_client", fake_traced)

    store = USPTokenStore()
    endpoint = "https://hvackoby.com/usp/v1"
    assert await store._discover_oauth_metadata(endpoint) is None
    assert store.oauth_unavailable(endpoint)
    assert await store._discover_oauth_metadata(endpoint) is None
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_get_token_skips_discovery_when_oauth_unavailable():
    store = USPTokenStore()
    endpoint = "https://biz.example.com/usp/v1"
    store._entries[endpoint] = {"oauth_unavailable": True}
    discover = AsyncMock(return_value=None)
    store._discover_and_register = discover  # type: ignore[method-assign]
    assert await store.get_token(endpoint) is None
    discover.assert_not_called()


@pytest.mark.asyncio
async def test_get_token_force_discovery_clears_oauth_unavailable(monkeypatch):
    store = USPTokenStore()
    endpoint = "https://biz.example.com/usp/v1"
    store._entries[endpoint] = {"oauth_unavailable": True}

    async def fake_discover(endpoint_arg: str):
        store._clear_oauth_unavailable(endpoint_arg)
        return ("cid", "csec", "https://biz.example.com/oauth2/token")

    async def fake_acquire(token_endpoint, client_id, client_secret):
        return ("access-tok", 9_999_999_999)

    store._discover_and_register = fake_discover  # type: ignore[method-assign]
    store._acquire_token = fake_acquire  # type: ignore[method-assign]

    token = await store.get_token(endpoint, force_discovery=True)
    assert token == "access-tok"
    assert not store.oauth_unavailable(endpoint)


@pytest.mark.asyncio
async def test_rest_client_retries_with_token_on_401(monkeypatch):
    endpoint = "https://biz.example.com/usp/v1"
    store = USPTokenStore()
    store._entries[endpoint] = {
        "client_id": "cid",
        "client_secret": "csec",
        "token_endpoint": "https://biz.example.com/oauth2/token",
        "access_token": "fresh-token",
        "expires_at": 9_999_999_999,
    }

    auth_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth_headers.append(request.headers.get("Authorization"))
        if len(auth_headers) == 1:
            return httpx.Response(401)
        return httpx.Response(
            200,
            json={
                "service": {
                    "id": "svc-1",
                    "business_id": "biz-1",
                    "name": "AC Repair",
                    "type": "appointment",
                    "duration": {"fixed": "PT1H"},
                    "pricing": {"model": "fixed", "amount": 100, "currency": "USD"},
                    "channel": {"type": "at_business_location"},
                    "policies": {
                        "cancellation": {"allowed": True},
                        "rescheduling": {"allowed": True},
                        "confirmation_mode": "auto",
                        "requires_payment": True,
                    },
                }
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_traced(**kwargs):
        merged = dict(kwargs)
        merged.setdefault("follow_redirects", True)
        return httpx.AsyncClient(transport=transport, **merged)

    monkeypatch.setattr("booking_agent.rest_client.traced_async_client", fake_traced)

    client = USPBusinessRestClient(
        _profile(),
        token_store=store,
        rest_endpoint=endpoint,
    )
    service = await client.get_service("svc-1")
    assert service.id == "svc-1"
    assert auth_headers == [None, "Bearer fresh-token"]


@pytest.mark.asyncio
async def test_live_client_skips_get_token_without_stored_credentials(monkeypatch):
    backend = LiveUSPBackend()
    backend._token_store.has_stored_credentials = MagicMock(return_value=False)  # type: ignore[method-assign]
    get_token = AsyncMock(return_value="should-not-run")
    backend._token_store.get_token = get_token  # type: ignore[method-assign]

    client = await backend._client(_profile())
    get_token.assert_not_called()
    assert client.bearer_token is None
    assert client._token_store is backend._token_store
