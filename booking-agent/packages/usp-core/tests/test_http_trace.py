"""Tests for ``booking_agent.http_trace``."""

import httpx
import pytest

from booking_agent.http_trace import traced_async_client


@pytest.mark.asyncio
async def test_traced_client_follows_redirect_without_request_not_read():
    """Redirect follow-up requests must not crash the request trace hook."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hvackoby.com":
            return httpx.Response(
                301,
                headers={
                    "Location": "https://www.hvackoby.com/.well-known/oauth-authorization-server",
                },
            )
        return httpx.Response(200, json={"token_endpoint": "https://www.example.com/oauth2/token"})

    transport = httpx.MockTransport(handler)
    async with traced_async_client(transport=transport, follow_redirects=True) as client:
        response = await client.get(
            "https://hvackoby.com/.well-known/oauth-authorization-server"
        )

    assert response.status_code == 200
    assert response.url.host == "www.hvackoby.com"
    assert response.json()["token_endpoint"] == "https://www.example.com/oauth2/token"


_ADDRESS_SENTINELS = {
    "line1": "SENTINEL-LINE1-7731",
    "line2": "SENTINEL-LINE2-7731",
    "city": "SENTINEL-CITY-7731",
    "region": "SENTINEL-REGION-7731",
    "postal_code": "SENTINEL-POSTAL-7731",
    "country": "SENTINEL-COUNTRY-7731",
}
_COOKIE_SENTINELS = (
    "SENTINEL-XSRF-7731",
    "SENTINEL-RECENT-WRITES-7731",
    "SENTINEL-WIXAB3-7731",
    "SENTINEL-REQ-COOKIE-7731",
)
_JOIN_IDS = (
    "df4da579-0aa2-4f0e-83a1-4d385e780b4b",
    "1760000000.123456789wix",
    "chk_join_7731",
    "bkg_join_7731",
    "ord_join_7731",
    "spr_join_7731",
    "svc_join_7731",
    "slot_join_7731",
    "confirmed",
    "EUR",
)


def _sentinel_address(prefix: str = "") -> dict:
    return {
        "line1": prefix + _ADDRESS_SENTINELS["line1"],
        "line2": prefix + _ADDRESS_SENTINELS["line2"],
        "city": prefix + _ADDRESS_SENTINELS["city"],
        "region": prefix + _ADDRESS_SENTINELS["region"],
        "postal_code": prefix + _ADDRESS_SENTINELS["postal_code"],
        "country": prefix + _ADDRESS_SENTINELS["country"],
        "coordinates": {"latitude": 32.0853771, "longitude": 34.7817676},
    }


async def test_trace_redacts_buyer_address_and_cookies_but_keeps_join_ids(
    tmp_path, monkeypatch
):
    from booking_agent import observability
    from booking_agent.http_trace import trace_path

    monkeypatch.setenv("BOOKING_AGENT_HTTP_TRACE", "1")
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))

    booking = {
        "id": "bkg_join_7731",
        "status": "confirmed",
        "service_id": "svc_join_7731",
        "slot": {"id": "slot_join_7731"},
        "deliveryAddress": _sentinel_address(),
    }
    response_body = {
        "id": "chk_join_7731",
        "status": "confirmed",
        "currency": "EUR",
        "totals": [{"type": "total", "amount": 11000}],
        "order": {"id": "ord_join_7731"},
        "spend_request_id": "spr_join_7731",
        "buyer": {"address": _sentinel_address()},
        "booking": booking,
        "shipping_address": [{"address": _sentinel_address()}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_body,
            headers=[
                ("x-wix-request-id", "1760000000.123456789wix"),
                ("Set-Cookie", f"XSRF-TOKEN={_COOKIE_SENTINELS[0]}; Path=/"),
                ("Set-Cookie", f"recent-writes={_COOKIE_SENTINELS[1]}; Path=/"),
                ("Set-Cookie", f"_wixAB3={_COOKIE_SENTINELS[2]}; Path=/"),
            ],
        )

    request_body = {
        "currency": "EUR",
        "line_items": [{"id": "li_1", "item": {"id": "svc_join_7731", "price": 10000}}],
        "booking": {
            "service_id": "svc_join_7731",
            "slot": {"id": "slot_join_7731"},
            "delivery_address": _sentinel_address(),
        },
    }

    token = observability.bind("df4da579-0aa2-4f0e-83a1-4d385e780b4b")
    try:
        async with traced_async_client(transport=httpx.MockTransport(handler)) as client:
            await client.post(
                "https://merchant.example/checkout-sessions",
                json=request_body,
                headers={"Cookie": f"session={_COOKIE_SENTINELS[3]}"},
            )
            await client.post(
                "https://merchant.example/raw",
                content=(
                    '{"delivery_address": {"line1": "raw-' + _ADDRESS_SENTINELS["line1"]
                    + '", "city": "raw-' + _ADDRESS_SENTINELS["city"]
                    + '", "postal_code": "raw-' + _ADDRESS_SENTINELS["postal_code"]
                    + '", "country": "raw-' + _ADDRESS_SENTINELS["country"]
                    + '"}, "id": "bkg_join_7731",'
                ),
            )
    finally:
        observability.reset(token)

    written = trace_path().read_text()
    leaked = [s for s in (*_ADDRESS_SENTINELS.values(), *_COOKIE_SENTINELS) if s in written]
    assert leaked == []
    assert "32.0853771" not in written
    for join_id in _JOIN_IDS:
        assert join_id in written
    assert "set-cookie" in written.lower()
    assert "x-wix-request-id" in written.lower()
