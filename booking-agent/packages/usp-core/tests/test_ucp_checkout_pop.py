"""Proofs of possession on UCP checkout (privileged checkout, USP Section 10.1.6).

``booking-agent flow book`` creates the booking through UCP ``create_checkout``, and that
is a privileged operation: it carries buyer personal data and ends in a charge.
The client used to send ``UCP-Agent``, ``UCP-Version`` and ``Idempotency-Key`` and
nothing else, so a business implementing ``platform_key_pop`` answered 401.
"""
from __future__ import annotations

import base64
import json
from typing import Any, Callable, Dict, List, Tuple

import httpx
import pytest

from booking_agent.pop import PopKey, PopKeyStore
from booking_agent.ucp_checkout import (
    UcpCheckoutClient,
    UcpCheckoutHttpError,
    build_ucp_checkout_client,
)

CHECKOUT_BASE = "https://merchant.example/ucp/v1"
PLATFORM_PROFILE = "https://profile.booking-agent.dev/platform-profile.json"


def _decode(segment: str) -> Dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _proof(request: httpx.Request) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    header, claims, _signature = request.headers["DPoP"].split(".")
    return _decode(header), _decode(claims)


def _ready_checkout() -> Dict[str, Any]:
    return {
        "id": "cs_1",
        "status": "ready_for_complete",
        "totals": [{"type": "total", "amount": 5000}],
        "links": [{"type": "terms", "url": "https://merchant.example/terms"}],
        "booking": {"booking_id": "bk_1"},
    }


def _completed_checkout() -> Dict[str, Any]:
    doc = _ready_checkout()
    doc["status"] = "completed"
    doc["booking"] = {"booking_id": "bk_1", "booking_status": "confirmed"}
    doc["order"] = {"id": "ord_1"}
    return doc


def _capture(
    monkeypatch, handler: Callable[[httpx.Request], httpx.Response],
) -> List[httpx.Request]:
    """Route checkout traffic through a mock transport, recording every attempt."""
    seen: List[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_record)

    def fake_traced(**kwargs):
        from booking_agent.http_trace import traced_async_client as real

        return real(**{**kwargs, "transport": transport})

    monkeypatch.setattr("booking_agent.ucp_checkout.traced_async_client", fake_traced)
    return seen


def _client(key: PopKey) -> UcpCheckoutClient:
    return UcpCheckoutClient(
        CHECKOUT_BASE,
        platform_profile_url=PLATFORM_PROFILE,
        ucp_version_header="2026-04-08",
        pop_key=key,
    )


@pytest.mark.asyncio
async def test_create_checkout_proves_possession_over_the_checkout_url(monkeypatch):
    key = PopKey.generate()
    requests = _capture(monkeypatch, lambda _r: httpx.Response(200, json=_ready_checkout()))

    await _client(key).create_checkout({"currency": "USD"})

    header, claims = _proof(requests[0])
    assert header == {"typ": "dpop+jwt", "alg": "EdDSA", "jwk": key.public_jwk}
    assert claims["htm"] == "POST"
    assert claims["htu"] == f"{CHECKOUT_BASE}/checkout-sessions"
    # Issuance shape: there is no credential to bind to at checkout, so no ``ath``.
    assert "ath" not in claims
    assert requests[0].headers["UCP-Agent"] == f'profile="{PLATFORM_PROFILE}"'


@pytest.mark.asyncio
async def test_complete_checkout_proves_possession_over_the_complete_url(monkeypatch):
    key = PopKey.generate()
    requests = _capture(
        monkeypatch, lambda _r: httpx.Response(200, json=_completed_checkout()),
    )

    await _client(key).complete_checkout("cs_1", {"payment": {"instruments": []}})

    _header, claims = _proof(requests[0])
    assert claims["htu"] == f"{CHECKOUT_BASE}/checkout-sessions/cs_1/complete"
    assert claims["htm"] == "POST"


@pytest.mark.asyncio
async def test_get_checkout_proves_possession(monkeypatch):
    key = PopKey.generate()
    requests = _capture(monkeypatch, lambda _r: httpx.Response(200, json=_ready_checkout()))

    await _client(key).get_checkout("cs_1")

    _header, claims = _proof(requests[0])
    assert claims["htm"] == "GET"
    assert claims["htu"] == f"{CHECKOUT_BASE}/checkout-sessions/cs_1"


@pytest.mark.asyncio
async def test_checkout_without_a_key_sends_no_proof(monkeypatch):
    requests = _capture(monkeypatch, lambda _r: httpx.Response(200, json=_ready_checkout()))

    await UcpCheckoutClient(CHECKOUT_BASE).create_checkout({"currency": "USD"})

    assert "DPoP" not in requests[0].headers


@pytest.mark.asyncio
async def test_each_attempt_carries_a_fresh_jti(monkeypatch):
    """A retry is a new proof: the business's replay cache rejects a reused ``jti``."""
    key = PopKey.generate()
    responses = [
        httpx.Response(503, json={"error": {"code": "unavailable"}}),
        httpx.Response(200, json=_ready_checkout()),
    ]
    requests = _capture(monkeypatch, lambda _r: responses.pop(0))

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("booking_agent.http_retry.asyncio.sleep", _no_sleep)

    await _client(key).create_checkout({"currency": "USD"}, idempotency_key="stable-1")

    assert len(requests) == 2
    first, second = (_proof(r)[1] for r in requests)
    assert first["jti"] != second["jti"]
    # The idempotency key is per logical call, so the business can still dedupe.
    assert {r.headers["Idempotency-Key"] for r in requests} == {"stable-1"}


@pytest.mark.asyncio
async def test_nonce_challenge_is_answered_once(monkeypatch):
    key = PopKey.generate()
    responses = [
        httpx.Response(
            401,
            headers={"DPoP-Nonce": "nonce-1"},
            json={"error": {"code": "proof_nonce_required"}},
        ),
        httpx.Response(200, json=_ready_checkout()),
    ]
    requests = _capture(monkeypatch, lambda _r: responses.pop(0))

    checkout = await _client(key).create_checkout({"currency": "USD"})

    assert checkout["id"] == "cs_1"
    assert len(requests) == 2
    assert "nonce" not in _proof(requests[0])[1]
    assert _proof(requests[1])[1]["nonce"] == "nonce-1"


@pytest.mark.asyncio
async def test_unauthorized_without_a_new_nonce_is_not_retried(monkeypatch):
    """A business that always answers 401 cannot spin the client."""
    key = PopKey.generate()
    requests = _capture(
        monkeypatch,
        lambda _r: httpx.Response(401, json={"error": {"code": "unauthorized"}}),
    )

    with pytest.raises(UcpCheckoutHttpError) as caught:
        await _client(key).create_checkout({"currency": "USD"})

    assert caught.value.status == 401
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_rotated_nonce_travels_on_the_next_call(monkeypatch):
    """RFC 9449 rotation: a nonce from any response is reused without a 401."""
    key = PopKey.generate()
    client = _client(key)
    requests = _capture(
        monkeypatch,
        lambda _r: httpx.Response(
            200, headers={"DPoP-Nonce": "nonce-2"}, json=_ready_checkout(),
        ),
    )

    await client.create_checkout({"currency": "USD"})
    await client.get_checkout("cs_1")

    assert "nonce" not in _proof(requests[0])[1]
    assert _proof(requests[1])[1]["nonce"] == "nonce-2"


def test_build_client_binds_the_key_of_the_checkout_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))

    client = build_ucp_checkout_client(
        CHECKOUT_BASE, platform_profile_url=PLATFORM_PROFILE, ucp_version_header="2026-04-08",
    )

    stored = PopKeyStore().peek(CHECKOUT_BASE)
    assert stored is not None
    assert client.pop_jkt == stored.jkt
    # A second checkout against the same business reuses the key, so a credential
    # bound to it stays usable.
    again = build_ucp_checkout_client(CHECKOUT_BASE, platform_profile_url=PLATFORM_PROFILE)
    assert again.pop_jkt == stored.jkt


def test_build_client_without_a_checkout_base_stores_no_key(monkeypatch, tmp_path):
    """An unresolved checkout base must not leave a key behind for no origin."""
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))

    client = build_ucp_checkout_client("", platform_profile_url=PLATFORM_PROFILE)

    assert client.pop_jkt is None
    assert not (tmp_path / "pop_keys.json").exists()


def test_build_client_omits_the_proof_when_authentication_is_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))

    client = build_ucp_checkout_client(
        CHECKOUT_BASE, platform_profile_url=PLATFORM_PROFILE, auth_posture="disabled",
    )

    assert client.pop_jkt is None


def _scoped_credential(key: PopKey):
    from datetime import datetime, timedelta, timezone

    from booking_agent.authorization import BookingScopedCredential

    return BookingScopedCredential.model_validate({
        "value": "bsc_" + ("c" * 24),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "cnf": {"jkt": key.jkt},
    })


@pytest.mark.asyncio
async def test_cancel_checkout_presents_the_scoped_credential(monkeypatch):
    """54862 only forwards what the agent sends; issuance-shape cancel is inner 401."""
    from booking_agent.pop import credential_ath

    key = PopKey.generate()
    credential = _scoped_credential(key)
    requests = _capture(monkeypatch, lambda _r: httpx.Response(200, json={"status": "canceled"}))

    await _client(key).cancel_checkout("cs_1", credential=credential)

    cancel = requests[0]
    assert str(cancel.url) == f"{CHECKOUT_BASE}/checkout-sessions/cs_1/cancel"
    assert cancel.headers["Authorization"] == f"DPoP {credential.value}"
    _header, claims = _proof(cancel)
    assert claims["ath"] == credential_ath(credential.value)
    assert claims["htm"] == "POST"
    assert claims["htu"] == f"{CHECKOUT_BASE}/checkout-sessions/cs_1/cancel"


@pytest.mark.asyncio
async def test_cancel_checkout_without_a_credential_stays_issuance_shaped(monkeypatch):
    key = PopKey.generate()
    requests = _capture(monkeypatch, lambda _r: httpx.Response(200, json={"status": "canceled"}))

    await _client(key).cancel_checkout("cs_1")

    assert "Authorization" not in requests[0].headers or not requests[0].headers.get(
        "Authorization", ""
    ).startswith("DPoP ")
    assert "ath" not in _proof(requests[0])[1]
