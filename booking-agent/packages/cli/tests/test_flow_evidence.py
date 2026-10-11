"""``booking-agent flow evidence`` join keys (Track A / issue #197)."""
from __future__ import annotations

import json

from booking_agent_cli.main import app
from booking_agent_cli.session import BookingRef, PaymentRef, Session, SessionStore, save_store


def test_flow_evidence_emits_join_keys_without_secrets(runner, isolated_session):
    session = Session(user_request="test")
    session.booking = BookingRef(id="bkg_evidence")
    session.ucp_checkout_session_id = "cs_test"
    session.payment = PaymentRef(
        spend_request_id="lsrq_test",
        order_id="ord_test",
        status="paid",
    )
    session.booking.credential = "bsc_secret"
    session.booking.endpoint = "https://biz.example"

    store = SessionStore(active_session_id=session.id, sessions=[session])
    save_store(store)

    result = runner.invoke(app, ["flow", "evidence"])
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    data = body["data"]
    assert data["spend_request_id"] == "lsrq_test"
    assert data["ucp_checkout_session_id"] == "cs_test"
    assert data["booking_id"] == "bkg_evidence"
    assert data["order_id"] == "ord_test"
    assert "bsc_" not in result.stdout
