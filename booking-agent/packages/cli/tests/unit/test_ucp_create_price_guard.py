"""create_checkout price guard: catalog price vs the business's line item, not the taxed total."""
from __future__ import annotations

import copy

import pytest

from booking_agent.observability import reset_event_sink, set_event_sink
from booking_agent_cli.payment_helpers import _finalize_ucp_checkout_create
from booking_agent_cli.session import BookingRef, Business, Session

_WIX_TAXED_CHECKOUT = {
    "id": "chk_tax",
    "status": "ready_for_complete",
    "currency": "USD",
    "line_items": [
        {"id": "li_1", "quantity": 1, "item": {"id": "svc", "title": "Home HVAC Repair", "price": 100}},
    ],
    "totals": [
        {"type": "subtotal", "amount": 100},
        {"type": "tax", "amount": 10},
        {"type": "total", "amount": 110},
    ],
    "links": [],
    "booking": {"booking_id": "bk_tax", "booking_status": "pending"},
}


class _Client:
    def __init__(self, checkout):
        self.checkout = checkout

    async def get_checkout(self, checkout_id, credential=None):
        return self.checkout


@pytest.fixture
def events(monkeypatch):
    captured: list[dict] = []
    set_event_sink(captured.append)
    monkeypatch.setattr(
        "booking_agent.ucp_capabilities.validate_checkout_capabilities", lambda *a, **k: {},
    )
    yield captured
    reset_event_sink()


def _session() -> Session:
    return Session(
        flow_phase="SLOT_SELECTED",
        business=Business(profile_url="https://biz.example/.well-known/ucp"),
        booking=BookingRef(id="bk_tax"),
        payment_route="at_booking",
    )


async def _finalize(session, checkout, expected=100):
    return await _finalize_ucp_checkout_create(
        session,
        _Client(checkout),
        checkout,
        checkout_id=checkout["id"],
        platform_supported={},
        negotiated_version="2026-04-08",
        expected_amount_minor=expected,
    )


@pytest.mark.asyncio
async def test_taxed_checkout_with_matching_line_item_is_accepted(events):
    session = _session()

    await _finalize(session, _WIX_TAXED_CHECKOUT)

    assert session.ucp_checkout_amount_minor == 110
    assert session.ucp_merchant_quote["line_item_price_minor_units"] == 100
    assert session.ucp_merchant_quote["tax_minor_units"] == 10
    assert not [e for e in events if e.get("event") == "price_mismatch"]


@pytest.mark.asyncio
async def test_line_item_price_that_differs_from_catalog_is_rejected(events):
    checkout = copy.deepcopy(_WIX_TAXED_CHECKOUT)
    checkout["line_items"][0]["item"]["price"] = 120

    with pytest.raises(RuntimeError, match="120 minor units \\(line item price\\)"):
        await _finalize(_session(), checkout)

    mismatch = [e for e in events if e.get("event") == "price_mismatch"]
    assert mismatch and mismatch[0]["compared"] == "line_item"
    assert mismatch[0]["expected"] == 100 and mismatch[0]["actual"] == 120


@pytest.mark.asyncio
async def test_subtotal_is_compared_when_no_line_items_are_echoed(events):
    checkout = copy.deepcopy(_WIX_TAXED_CHECKOUT)
    del checkout["line_items"]

    await _finalize(_session(), checkout)

    checkout["totals"][0]["amount"] = 90
    with pytest.raises(RuntimeError, match="subtotal"):
        await _finalize(_session(), checkout)


@pytest.mark.asyncio
async def test_total_is_compared_only_when_neither_line_item_nor_subtotal_is_sent(events):
    checkout = copy.deepcopy(_WIX_TAXED_CHECKOUT)
    del checkout["line_items"]
    checkout["totals"] = [{"type": "total", "amount": 110}]

    with pytest.raises(RuntimeError, match="110 minor units \\(total\\)"):
        await _finalize(_session(), checkout)

    mismatch = [e for e in events if e.get("event") == "price_mismatch"]
    assert mismatch[-1]["compared"] == "total"
