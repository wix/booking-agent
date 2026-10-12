import httpx
import pytest

from booking_agent.rest_client import USPTokenStore


@pytest.mark.asyncio
async def test_oauth_metadata_follows_redirect(monkeypatch):
    """RFC 8414 discovery must follow apex-to-www redirects (hvackoby.com case)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hvackoby.com":
            return httpx.Response(
                301,
                headers={
                    "Location": "https://www.hvackoby.com/.well-known/oauth-authorization-server",
                },
            )
        return httpx.Response(
            200,
            json={
                "token_endpoint": "https://www.hvackoby.com/oauth2/token",
                "registration_endpoint": "https://www.hvackoby.com/oauth2/register",
            },
        )

    transport = httpx.MockTransport(handler)

    def fake_traced(**kwargs):
        merged = dict(kwargs)
        merged.setdefault("follow_redirects", True)
        return httpx.AsyncClient(transport=transport, **merged)

    monkeypatch.setattr("booking_agent.rest_client.traced_async_client", fake_traced)

    store = USPTokenStore()
    metadata = await store._discover_oauth_metadata("https://hvackoby.com/usp/v1")
    assert metadata is not None
    assert metadata["token_endpoint"] == "https://www.hvackoby.com/oauth2/token"
