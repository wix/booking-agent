"""Retries: one idempotency key, a fresh proof each attempt.

These pull in opposite directions and both matter.  A retry is another attempt
at the *same* logical call, so the business must be able to deduplicate it --
which needs a stable ``Idempotency-Key``.  But the replay cache rejects a
``jti`` it has already seen, so the proof must be rebuilt.  Before this change
``_headers`` generated the idempotency key per attempt, meaning a retried
booking create was not deduplicated at all: a network blip could double-book.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from booking_agent.discovery import USPBusinessInfo, USPBusinessProfile, USPTransportBinding
from booking_agent.pop import PopKey
from booking_agent.rest_client import USPBusinessRestClient

ENDPOINT = "https://biz.example.com/usp/v1"

_BOOKING_OK = {
    "booking": {
        "id": "bkg_001",
        "service_id": "svc_001",
        "service_name": "AC Repair",
        "slot": {
            "id": "slot_001",
            "start": "2026-06-05T10:00:00Z",
            "end": "2026-06-05T11:00:00Z",
            "duration": "PT1H",
        },
        "buyer": {
            "first_name": "Ran",
            "last_name": "Y",
            "email": "ran@example.com",
            "phone_number": "",
        },
        "party_size": 1,
        "status": "confirmed",
        "confirmation_mode": "auto",
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-01T00:00:00Z",
    }
}


def _profile() -> USPBusinessProfile:
    return USPBusinessProfile(
        version="2026-08-14",
        services={
            "dev.usp-protocol.services": [
                USPTransportBinding(
                    version="2026-08-14", transport="rest", endpoint=ENDPOINT,
                )
            ]
        },
        capabilities={},
        checkout_systems=[],
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )


def _claims(proof: str) -> dict:
    segment = proof.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _install_flaky(monkeypatch, success_json: dict) -> list[httpx.Request]:
    """First attempt fails with a retryable 503; the second succeeds."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=success_json)

    transport = httpx.MockTransport(handler)

    def fake_traced(**kwargs):
        merged = dict(kwargs)
        merged.setdefault("follow_redirects", True)
        return httpx.AsyncClient(transport=transport, **merged)

    monkeypatch.setattr("booking_agent.rest_client.traced_async_client", fake_traced)
    monkeypatch.setattr("booking_agent.http_retry.asyncio.sleep", _no_sleep)
    return requests


@pytest.fixture
def flaky(monkeypatch):
    return _install_flaky(monkeypatch, {"hold_id": "h1", "status": "released"})


async def _no_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_retry_reuses_the_idempotency_key_but_not_the_proof(flaky):
    key = PopKey.generate()
    client = USPBusinessRestClient(
        _profile(), agent_profile_url=None, pop_key=key, authenticate=True,
    )
    await client.release_hold("h1")

    assert len(flaky) == 2, "expected one retry"
    first, second = flaky

    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"], (
        "a retried call must reuse its idempotency key or the business cannot "
        "deduplicate it -- this is how a network blip becomes a double booking"
    )
    assert _claims(first.headers["DPoP"])["jti"] != _claims(second.headers["DPoP"])["jti"], (
        "a retried call must carry a fresh jti or the replay cache rejects it"
    )


@pytest.mark.asyncio
async def test_create_booking_retries_reuse_the_idempotency_key_but_not_the_proof(monkeypatch):
    """Issue 163 AC 2: POST /bookings retries must reuse Idempotency-Key."""
    requests = _install_flaky(monkeypatch, _BOOKING_OK)
    key = PopKey.generate()
    client = USPBusinessRestClient(
        _profile(), agent_profile_url=None, pop_key=key, authenticate=True,
    )
    await client.create_booking(
        "svc_001",
        "slot_001",
        {"first_name": "Ran", "last_name": "Y", "email": "ran@example.com"},
    )

    assert len(requests) == 2, "expected one retry"
    first, second = requests
    assert first.method == "POST" and first.url.path.endswith("/bookings")
    assert second.method == "POST" and second.url.path.endswith("/bookings")

    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"], (
        "a retried POST /bookings must reuse its idempotency key or the business "
        "cannot deduplicate it -- this is how a network blip becomes a double booking"
    )
    assert _claims(first.headers["DPoP"])["jti"] != _claims(second.headers["DPoP"])["jti"], (
        "a retried call must carry a fresh jti or the replay cache rejects it"
    )
