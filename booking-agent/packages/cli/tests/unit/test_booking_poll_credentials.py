from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from booking_agent.authorization import BookingScopedCredential
from booking_agent.pop import PopKey
from booking_agent_cli.booking_poll import _fetch_booking_async
from booking_agent_cli.session import BookingRef, Business, Session, record_issued_credential


USP_ENDPOINT = "https://usp.biz.example/usp/v1"
CHECKOUT_ENDPOINT = "https://checkout.biz.example/ucp/v1"


def _credential(key: PopKey) -> BookingScopedCredential:
    return BookingScopedCredential.model_validate({
        "value": "bsc_poll_credential_value_x",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "cnf": {"jkt": key.jkt},
    })


@pytest.mark.asyncio
async def test_booking_poll_presents_checkout_credential_on_usp_binding(monkeypatch):
    session = Session(
        business=Business(
            profile_url="https://biz.example/.well-known/ucp",
            rest_endpoint=USP_ENDPOINT,
        ),
        booking=BookingRef(id="bk_1"),
        ucp_checkout_rest_base=CHECKOUT_ENDPOINT,
    )
    record_issued_credential(
        session,
        _credential(PopKey.generate()),
        endpoint=CHECKOUT_ENDPOINT,
    )
    captured = {}

    class Backend:
        async def resolve_profile(self, _profile_url):
            return SimpleNamespace(rest_endpoint=USP_ENDPOINT)

        async def get_booking(
            self,
            _profile,
            booking_id,
            *,
            credential,
            credential_endpoint,
        ):
            captured["booking_id"] = booking_id
            captured["credential"] = credential
            captured["credential_endpoint"] = credential_endpoint
            return SimpleNamespace(id=booking_id, status="confirmed")

    monkeypatch.setattr(
        "booking_agent_cli.booking_poll.resolve_backend",
        lambda: Backend(),
    )

    booking = await _fetch_booking_async(session, "bk_1")

    assert booking.id == "bk_1"
    assert captured["credential"].value == "bsc_poll_credential_value_x"
    assert captured["credential_endpoint"] == CHECKOUT_ENDPOINT
