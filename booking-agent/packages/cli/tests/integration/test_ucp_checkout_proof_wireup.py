"""``booking-agent flow book`` and ``flow pay`` must prove possession on UCP checkout.

The client-side mechanism is covered in ``usp-core``; what shipped broken was the
wire-up. Both commands built a ``UcpCheckoutClient`` with no key, so the outgoing
``POST /checkout-sessions`` carried no ``DPoP`` header and a business
implementing ``platform_key_pop`` answered 401.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import httpx
import pytest

from booking_agent.pop import PopKeyStore, credential_ath

from booking_agent.ucp_checkout import UcpCheckoutHttpError
from booking_agent.ucp_profile import UcpProfileValidationError
from booking_agent.models.wire import (
    CancellationPolicy,
    Channel,
    Duration,
    Pricing,
    ReschedulingPolicy,
    ServicePolicies,
    USPService,
)
from booking_agent_cli.payment_helpers import (
    _create_booking_via_ucp_checkout,
    complete_ucp_checkout_async,
)
from booking_agent_cli.session import BookingRef, Business, Buyer, PaymentRef, Session, SlotRef
from support.mock.link_cli import fake_shared_payment_token

CHECKOUT_BASE = "https://checkout.example/ucp"
BIZ_PROFILE = "https://biz.example/.well-known/ucp"


def _proof_claims(request: httpx.Request) -> Dict[str, Any]:
    _header, claims, _signature = request.headers["DPoP"].split(".")
    return json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))


def _service() -> USPService:
    return USPService(
        id="svc_1",
        business_id="biz_1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT60M"),
        pricing=Pricing(model="fixed", amount=5000, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            confirmation_mode="auto",
            requires_payment=True,
        ),
    )


def _session() -> Session:
    return Session(
        flow_phase="SLOT_SELECTED",
        business=Business(profile_url=BIZ_PROFILE, name="AC Co"),
        buyer=Buyer(name="Ada Lovelace", email="ada@example.test"),
        booking=BookingRef(),
        payment=PaymentRef(spend_request_id="sr_1", status="pending"),
        checkout_mode="ucp_native",
        service_id="svc_1",
        slot_id="slot_1",
        slots_presented=[
            SlotRef(
                slot_id="slot_1",
                start="2026-10-01T10:00:00Z",
                end="2026-10-01T11:00:00Z",
                duration="PT60M",
            )
        ],
        ucp_checkout_rest_base=CHECKOUT_BASE,
    )


def _ready_checkout() -> Dict[str, Any]:
    return {
        "id": "cs_1",
        "status": "ready_for_complete",
        "totals": [{"type": "total", "amount": 5000}],
        "links": [{"type": "terms", "url": "https://biz.example/terms"}],
        "booking": {"booking_id": "bk_1"},
        "payment": {"instruments": [{"handler_id": "stripe_payments"}]},
        "ucp": {
            "payment_handlers": {
                "com.stripe.payments": [
                    {
                        "id": "stripe_payments",
                        "available_instruments": [
                            {"type": "link", "config": {"network_id": "ntwrk_1"}}
                        ],
                    }
                ]
            }
        },
    }


def _completed_checkout() -> Dict[str, Any]:
    doc = _ready_checkout()
    doc["status"] = "completed"
    doc["booking"] = {"booking_id": "bk_1", "booking_status": "confirmed"}
    doc["order"] = {"id": "ord_1"}
    return doc


def _stub_checkout_http(monkeypatch, handler) -> List[httpx.Request]:
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


def _stub_negotiation(monkeypatch) -> None:
    async def _platform_doc(_url):
        return {"ucp": {}}

    async def _biz_doc(_url):
        return {"ucp": {}}

    monkeypatch.setattr("booking_agent.ucp_negotiation.load_platform_profile", _platform_doc)
    monkeypatch.setattr("booking_agent_cli.payment_helpers._fetch_json", _biz_doc)
    monkeypatch.setattr(
        "booking_agent.ucp_negotiation.negotiate_ucp_protocol_version",
        lambda *_args, **_kwargs: "2026-04-08",
    )
    monkeypatch.setattr(
        "booking_agent.ucp_negotiation.intersect_ucp_capabilities",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "booking_agent.ucp_capabilities.validate_checkout_capabilities",
        lambda *_args, **_kwargs: {},
    )


def _stub_negotiation_fail_closed(monkeypatch) -> None:
    """Same profile stubs as live book, but keep the real capability gate."""

    async def _platform_doc(_url):
        return {"ucp": {}}

    async def _biz_doc(_url):
        return {"ucp": {}}

    monkeypatch.setattr("booking_agent.ucp_negotiation.load_platform_profile", _platform_doc)
    monkeypatch.setattr("booking_agent_cli.payment_helpers._fetch_json", _biz_doc)
    monkeypatch.setattr(
        "booking_agent.ucp_negotiation.negotiate_ucp_protocol_version",
        lambda *_args, **_kwargs: "2026-04-08",
    )
    monkeypatch.setattr(
        "booking_agent.ucp_negotiation.intersect_ucp_capabilities",
        lambda *_args, **_kwargs: {
            "dev.ucp.shopping.checkout": ["2026-04-08"],
            "dev.usp-protocol.services.paid_bookings": ["2026-08-20"],
        },
    )


def _issued_credential_for_checkout_origin() -> Dict[str, Any]:
    key = PopKeyStore().peek(CHECKOUT_BASE)
    assert key is not None
    return {
        "value": "bsc_" + ("c" * 24),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "cnf": {"jkt": key.jkt},
    }


def _static_map_checkout(*, issued_credential: bool = False) -> Dict[str, Any]:
    """Shape of the production HVAC-Koby 201 capability echo."""
    doc = _ready_checkout()
    ucp = dict(doc["ucp"])
    ucp["version"] = "2026-04-08"
    ucp["capabilities"] = {
        "dev.ucp.shopping.checkout": ["2026-04-08"],
        "dev.ucp.shopping.fulfillment": ["2026-04-08"],
        "dev.ucp.shopping.catalog.search": ["2026-04-08"],
        "dev.ucp.shopping.catalog.lookup": ["2026-04-08"],
    }
    doc["ucp"] = ucp
    if issued_credential:
        doc["booking_scoped_credential"] = _issued_credential_for_checkout_origin()
    return doc


def _backend_stub(monkeypatch) -> None:
    class _Backend:
        async def resolve_profile(self, _url):
            return object()

        async def get_service(self, _profile, _service_id):
            return _service()

    monkeypatch.setattr("booking_agent_cli.payment_helpers.resolve_backend", lambda: _Backend())


@pytest.mark.asyncio
async def test_book_sends_a_proof_on_create_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", "https://agent.example/platform-profile.json")
    _stub_negotiation(monkeypatch)
    requests = _stub_checkout_http(
        monkeypatch, lambda _r: httpx.Response(200, json=_ready_checkout()),
    )

    class _Backend:
        async def resolve_profile(self, _url):
            return object()

        async def get_service(self, _profile, _service_id):
            return _service()

    monkeypatch.setattr("booking_agent_cli.payment_helpers.resolve_backend", lambda: _Backend())

    session = _session()
    await _create_booking_via_ucp_checkout(session, "svc_1", None)

    post = requests[0]
    assert post.method == "POST"
    assert str(post.url) == f"{CHECKOUT_BASE}/checkout-sessions"
    claims = _proof_claims(post)
    assert claims["htm"] == "POST"
    assert claims["htu"] == f"{CHECKOUT_BASE}/checkout-sessions"
    assert (
        post.headers["UCP-Agent"]
        == 'profile="https://agent.example/platform-profile.json"'
    )


@pytest.mark.asyncio
async def test_pay_sends_a_proof_on_complete_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", "https://agent.example/platform-profile.json")
    _stub_negotiation(monkeypatch)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_ready_checkout())
        return httpx.Response(200, json=_completed_checkout())

    requests = _stub_checkout_http(monkeypatch, _handler)
    fake_shared_payment_token(monkeypatch, "spt_test")

    session = _session()
    session.ucp_checkout_session_id = "cs_1"
    session.ucp_checkout_amount_minor = 5000

    result = await complete_ucp_checkout_async(session)

    assert result["order_id"] == "ord_1"
    complete = requests[-1]
    assert str(complete.url) == f"{CHECKOUT_BASE}/checkout-sessions/cs_1/complete"
    assert _proof_claims(complete)["htu"] == (
        f"{CHECKOUT_BASE}/checkout-sessions/cs_1/complete"
    )
    # The read that precedes it is equally privileged: it returns buyer data.
    assert "DPoP" in requests[0].headers


@pytest.mark.asyncio
async def test_rejected_checkout_persists_ids_and_cancels(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", "https://agent.example/platform-profile.json")
    _stub_negotiation_fail_closed(monkeypatch)
    _backend_stub(monkeypatch)

    def _handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if request.method == "POST" and path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "canceled"})
        return httpx.Response(201, json=_static_map_checkout(issued_credential=True))

    requests = _stub_checkout_http(monkeypatch, _handler)
    session = _session()

    with pytest.raises(UcpProfileValidationError) as exc_info:
        await _create_booking_via_ucp_checkout(session, "svc_1", None)

    message = str(exc_info.value)
    assert "dev.ucp.shopping.fulfillment" in message
    assert "dev.ucp.shopping.catalog.search" in message
    assert "dev.ucp.shopping.catalog.lookup" in message
    assert session.ucp_checkout_session_id == "cs_1"
    assert session.booking.id == "bk_1"
    assert session.booking.checkout_path != "ucp"
    assert session.ucp_capabilities_authoritative is False
    assert session.ucp_checkout_cancel_attempted is True
    assert session.ucp_checkout_cancel_succeeded is True
    assert session.booking.credential == "bsc_" + ("c" * 24)
    cancel = next(
        r for r in requests
        if r.method == "POST" and str(r.url).endswith("/checkout-sessions/cs_1/cancel")
    )
    assert cancel.headers["Authorization"] == "DPoP " + "bsc_" + ("c" * 24)
    assert _proof_claims(cancel)["ath"] == credential_ath("bsc_" + ("c" * 24))


@pytest.mark.asyncio
async def test_cancel_failure_preserves_capability_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", "https://agent.example/platform-profile.json")
    _stub_negotiation_fail_closed(monkeypatch)
    _backend_stub(monkeypatch)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and str(request.url).endswith("/cancel"):
            return httpx.Response(500, text="cancel exploded")
        return httpx.Response(201, json=_static_map_checkout(issued_credential=True))

    _stub_checkout_http(monkeypatch, _handler)
    session = _session()

    with pytest.raises(UcpProfileValidationError) as exc_info:
        await _create_booking_via_ucp_checkout(session, "svc_1", None)

    assert "fulfillment" in str(exc_info.value)
    assert not isinstance(exc_info.value, UcpCheckoutHttpError)
    assert session.ucp_checkout_session_id == "cs_1"
    assert session.booking.id == "bk_1"
    assert session.ucp_checkout_cancel_attempted is True
    assert session.ucp_checkout_cancel_succeeded is False
    assert session.ucp_checkout_cancel_error
