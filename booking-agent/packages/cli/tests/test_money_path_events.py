"""Money-path observability events from UCP checkout."""
from __future__ import annotations

import pytest

from booking_agent.observability import reset_event_sink, set_event_sink
from booking_agent.ucp_checkout import UcpCheckoutHttpError, UcpProfileValidationError
from booking_agent_cli.payment_helpers import complete_ucp_checkout_async
from booking_agent_cli.session import BookingRef, Business, PaymentRef, Session
from support.mock_link_cli import fake_shared_payment_token


def _ucp_session() -> Session:
    return Session(
        flow_phase="PAYMENT_PENDING",
        business=Business(profile_url="https://biz.example/.well-known/usp"),
        booking=BookingRef(id="bk_1"),
        payment=PaymentRef(spend_request_id="sr_1", status="pending"),
        ucp_checkout_session_id="chk_1",
        ucp_checkout_rest_base="https://checkout.example/ucp",
        ucp_checkout_amount_minor=5000,
        ucp_capabilities_authoritative=True,
        ucp_protocol_version="2026-04-08",
        ucp_negotiated_capabilities={"dev.ucp.paid_bookings": ["2026-04-08"]},
    )


@pytest.mark.asyncio
async def test_complete_checkout_decline_emits_payment_declined(monkeypatch):
    captured: list[dict] = []
    set_event_sink(captured.append)
    session = _ucp_session()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_checkout(self, checkout_id: str):
            return {
                "id": checkout_id,
                "status": "ready_for_payment",
                "payment": {"instruments": [{"handler_id": "h1"}]},
                "ucp": {"capabilities": session.ucp_negotiated_capabilities, "version": "2026-04-08"},
            }

        async def complete_checkout(self, checkout_id, body, *, idempotency_key=None):
            raise UcpCheckoutHttpError(402, "card declined")

    fake_shared_payment_token(monkeypatch, "spt_test")
    monkeypatch.setattr("booking_agent_cli.payment_helpers._fetch_json", lambda _url: {"ucp": {}})
    monkeypatch.setattr("booking_agent.ucp_checkout.UcpCheckoutClient", FakeClient)
    monkeypatch.setattr(
        "booking_agent.ucp_checkout.resolve_handler_id_for_ucp_complete",
        lambda **kwargs: "h1",
    )
    monkeypatch.setattr(
        "booking_agent.ucp_checkout.build_payment_instrument_for_complete_checkout",
        lambda *args, **kwargs: {
            "handler_id": "stripe_payments",
            "type": "link",
            "credential": {"type": "stripe_payment_token", "token": "spt_test"},
        },
    )
    monkeypatch.setattr("booking_agent.ucp_capabilities.validate_checkout_capabilities", lambda *args, **kwargs: {})

    with pytest.raises(UcpCheckoutHttpError):
        await complete_ucp_checkout_async(session)

    assert any(e.get("event") == "charge_attempt" for e in captured)
    declined = [e for e in captured if e.get("event") == "payment_declined"]
    assert declined
    assert declined[0]["reason_class"] == "declined"
    assert "email" not in declined[0]
    reset_event_sink()


@pytest.mark.asyncio
async def test_complete_checkout_unconfirmed_booking_emits_event(monkeypatch):
    captured: list[dict] = []
    set_event_sink(captured.append)
    session = _ucp_session()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_checkout(self, checkout_id: str):
            return {
                "id": checkout_id,
                "status": "ready_for_payment",
                "payment": {"instruments": [{"handler_id": "h1"}]},
                "ucp": {"capabilities": session.ucp_negotiated_capabilities, "version": "2026-04-08"},
            }

        async def complete_checkout(self, checkout_id, body, *, idempotency_key=None):
            return {
                "status": "completed",
                "booking": {"booking_status": "pending"},
                "order": {"id": "ord_1"},
                "totals": {"total": 5000},
            }

    fake_shared_payment_token(monkeypatch, "spt_test")
    monkeypatch.setattr("booking_agent_cli.payment_helpers._fetch_json", lambda _url: {"ucp": {}})
    monkeypatch.setattr("booking_agent.ucp_checkout.UcpCheckoutClient", FakeClient)
    monkeypatch.setattr(
        "booking_agent.ucp_checkout.resolve_handler_id_for_ucp_complete",
        lambda **kwargs: "h1",
    )
    monkeypatch.setattr(
        "booking_agent.ucp_checkout.build_payment_instrument_for_complete_checkout",
        lambda *args, **kwargs: {
            "handler_id": "stripe_payments",
            "type": "link",
            "credential": {"type": "stripe_payment_token", "token": "spt_test"},
        },
    )
    monkeypatch.setattr("booking_agent.ucp_capabilities.validate_checkout_capabilities", lambda *args, **kwargs: {})

    with pytest.raises(UcpProfileValidationError):
        await complete_ucp_checkout_async(session)

    assert any(e.get("event") == "payment_captured_booking_unconfirmed" for e in captured)
    reset_event_sink()


_DEPOSIT_TERM = {
    "id": "pt_fixed_deposit",
    "title": "Deposit now, balance at the appointment",
    "schedules": [
        {"id": "sched_deposit", "type": "immediate", "amount": 1000},
        {
            "id": "sched_balance",
            "type": "at_service",
            "due_at": "2026-10-14T10:00:00-04:00",
            "amount": 9000,
        },
    ],
}


def _deposit_session() -> Session:
    session = _ucp_session()
    session.payment_route = "deposit_required"
    session.ucp_checkout_amount_minor = 10000
    session.due_now_amount_minor = 1000
    session.accepted_term = _DEPOSIT_TERM
    session.selected_term_id = "pt_fixed_deposit"
    session.ucp_negotiated_capabilities = {
        "dev.ucp.common.payment.terms": ["2026-08-25"],
    }
    return session


def _install_deposit_complete(monkeypatch, completed: dict) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def get_checkout(self, checkout_id: str):
            return {
                "id": checkout_id,
                "status": "ready_for_complete",
                "currency": "USD",
                "totals": [{"type": "total", "amount": 10000}],
                "payment": {
                    "selected_term_id": "pt_fixed_deposit",
                    "terms": [_DEPOSIT_TERM],
                    "instruments": [{"handler_id": "h1"}],
                },
            }

        async def complete_checkout(self, checkout_id, body, *, idempotency_key=None):
            return completed

    fake_shared_payment_token(monkeypatch, "spt_test")
    monkeypatch.setattr("booking_agent.ucp_checkout.UcpCheckoutClient", FakeClient)
    monkeypatch.setattr(
        "booking_agent.ucp_checkout.resolve_handler_id_for_ucp_complete",
        lambda **kwargs: "h1",
    )
    monkeypatch.setattr(
        "booking_agent.ucp_checkout.build_payment_instrument_for_complete_checkout",
        lambda *args, **kwargs: {
            "handler_id": "stripe_payments",
            "type": "link",
            "credential": {"type": "stripe_payment_token", "token": "spt_test"},
        },
    )
    monkeypatch.setattr(
        "booking_agent.ucp_capabilities.validate_checkout_capabilities",
        lambda *args, **kwargs: {},
    )


def _wix_completed_checkout(*, selected_term_id: str) -> dict:
    """Current Wix complete body: selected term id and an order pointer."""
    return {
        "status": "completed",
        "totals": [{"type": "total", "amount": 10000}],
        "links": [{"rel": "privacy_policy", "url": "https://biz.example/privacy"}],
        "booking": {"booking_id": "bk_1", "booking_status": "confirmed"},
        "payment": {"selected_term_id": selected_term_id},
        "order": {
            "id": "ord_1",
            "permalink_url": "https://biz.example/orders/ord_1",
        },
    }


@pytest.mark.asyncio
async def test_complete_checkout_accepts_wix_order_pointer_when_term_id_matches(monkeypatch):
    captured: list[dict] = []
    set_event_sink(captured.append)
    session = _deposit_session()
    _install_deposit_complete(
        monkeypatch,
        _wix_completed_checkout(selected_term_id="pt_fixed_deposit"),
    )

    result = await complete_ucp_checkout_async(session)

    assert result["order_id"] == "ord_1"
    assert any(e.get("event") == "charge_attempt" for e in captured)
    reset_event_sink()


@pytest.mark.asyncio
async def test_complete_checkout_rejects_a_changed_selected_term_id(monkeypatch):
    captured: list[dict] = []
    set_event_sink(captured.append)
    session = _deposit_session()
    _install_deposit_complete(
        monkeypatch,
        _wix_completed_checkout(selected_term_id="pt_other"),
    )

    with pytest.raises(UcpProfileValidationError, match="does not match"):
        await complete_ucp_checkout_async(session)

    assert any(e.get("event") == "charge_attempt" for e in captured)
    assert not any(e.get("event") == "payment_captured_booking_unconfirmed" for e in captured)
    reset_event_sink()
