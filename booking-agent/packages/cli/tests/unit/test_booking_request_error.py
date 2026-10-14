"""A refused ``POST /bookings`` keeps only the HTTP status and a safe business code."""
from __future__ import annotations

import httpx
import pytest

from booking_agent.pop import PopKey
from booking_agent.rest_client import BookingRequestError, USPBusinessRestClient
from integration.test_request_retry_semantics import _BOOKING_OK, _no_sleep, _profile

_BUYER = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"}
_LEAKY_TEXT = "Ada Lovelace at 221B Baker Street"


def _install(monkeypatch, *responses: httpx.Response) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses[min(len(requests), len(responses)) - 1]

    transport = httpx.MockTransport(handler)

    def fake_traced(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr("booking_agent.rest_client.traced_async_client", fake_traced)
    monkeypatch.setattr("booking_agent.http_retry.asyncio.sleep", _no_sleep)
    return requests


def _client() -> USPBusinessRestClient:
    return USPBusinessRestClient(
        _profile(), agent_profile_url=None, pop_key=PopKey.generate(), authenticate=True,
    )


def _wix_error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={
        "message": f"agent profile URI refused: UNRESOLVABLE ({_LEAKY_TEXT})",
        "details": {"applicationError": {"code": code, "description": _LEAKY_TEXT}},
    })


@pytest.mark.parametrize(
    "response,code",
    [
        (_wix_error(400, "invalid_profile_url"), "invalid_profile_url"),
        (httpx.Response(409, json={"error": {"code": "slot_unavailable", "content": _LEAKY_TEXT}}),
         "slot_unavailable"),
        (httpx.Response(422, json={"messages": [
            {"type": "warning", "code": "late_arrival"},
            {"type": "error", "code": "buyer_invalid", "content": _LEAKY_TEXT},
        ]}), "buyer_invalid"),
    ],
    ids=["wix_application_error", "spec_error_object", "messages_envelope"],
)
@pytest.mark.asyncio
async def test_coded_rejection_keeps_status_and_code_only(monkeypatch, response, code):
    _install(monkeypatch, response)

    with pytest.raises(BookingRequestError) as caught:
        await _client().create_booking("svc_001", "slot_001", _BUYER)

    assert caught.value.http_status == response.status_code
    assert caught.value.business_code == code
    assert str(caught.value) == (
        f"The business rejected the booking (HTTP {response.status_code}, code {code})."
    )
    assert "Ada" not in str(caught.value) and "Baker" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": {"code": _LEAKY_TEXT}}),
        httpx.Response(400, json={"message": "no code here"}),
        httpx.Response(400, text="<html>bad gateway</html>"),
    ],
    ids=["unsafe_code", "no_code", "not_json"],
)
@pytest.mark.asyncio
async def test_rejection_without_a_safe_code_stays_an_http_error(monkeypatch, response):
    _install(monkeypatch, response)

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await _client().create_booking("svc_001", "slot_001", _BUYER)

    assert not isinstance(caught.value, BookingRequestError)


@pytest.mark.asyncio
async def test_caller_key_is_sent_on_every_attempt(monkeypatch):
    requests = _install(
        monkeypatch, _wix_error(400, "invalid_profile_url"), httpx.Response(200, json=_BOOKING_OK),
    )
    client = _client()

    with pytest.raises(BookingRequestError):
        await client.create_booking("svc_001", "slot_001", _BUYER, idempotency_key="key-1")
    await client.create_booking("svc_001", "slot_001", _BUYER, idempotency_key="key-1")

    assert [r.headers["Idempotency-Key"] for r in requests] == ["key-1", "key-1"]


@pytest.mark.asyncio
async def test_without_a_caller_key_each_call_gets_its_own(monkeypatch):
    requests = _install(monkeypatch, httpx.Response(200, json=_BOOKING_OK))
    client = _client()

    await client.create_booking("svc_001", "slot_001", _BUYER)
    await client.create_booking("svc_001", "slot_001", _BUYER)

    first, second = (r.headers["Idempotency-Key"] for r in requests)
    assert first and second and first != second
