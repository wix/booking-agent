"""Pay-at-service checkout (USP 11.3): settlement validation, routing, and capability detection."""
from __future__ import annotations

import copy

import pytest

from booking_agent.ucp_checkout import (
    UcpProfileValidationError,
    is_offline_settled,
    validate_offline_settlement,
    validate_payment_terms,
)
from booking_agent.ucp_profile import parse_ucp_native_context_from_document
from booking_agent_cli.catalog import (
    classify_payment_route,
    payment_timing_changed_refusal,
)

_SLOT = "2026-10-14T10:00:00-04:00"
_CAPS = {
    "dev.ucp.shopping.checkout": ["2026-08-25"],
    "dev.ucp.common.payment.terms": ["2026-08-25"],
    "dev.usp-protocol.services.pay_at_service": ["2026-08-20"],
}


def _checkout(**overrides):
    checkout = {
        "id": "checkout_9f2",
        "status": "ready_for_complete",
        "currency": "USD",
        "totals": [{"type": "total", "amount": 7500}],
        "payment": {
            "selected_term_id": "pt_at_service",
            "terms": [
                {
                    "id": "pt_at_service",
                    "title": "Pay at your appointment",
                    "schedules": [
                        {
                            "id": "sched_at_service",
                            "type": "at_service",
                            "description": {"plain": "Due at your appointment."},
                            "due_at": _SLOT,
                            "amount": 7500,
                        }
                    ],
                }
            ],
        },
        "settlement": {"mode": "offline", "term_id": "pt_at_service"},
        "booking": {"slot": {"start": _SLOT}},
    }
    checkout.update(overrides)
    return checkout


def _validate(checkout, **kwargs):
    kwargs.setdefault("negotiated_capabilities", _CAPS)
    kwargs.setdefault("slot_start", _SLOT)
    kwargs.setdefault("expect_offline_settlement", True)
    return validate_payment_terms(checkout, **kwargs)


def test_valid_offline_checkout_returns_the_named_term():
    term = _validate(_checkout())
    assert term["id"] == "pt_at_service"
    assert is_offline_settled(_checkout())


def test_due_at_may_differ_in_offset_spelling_but_not_in_instant():
    checkout = _checkout()
    checkout["payment"]["terms"][0]["schedules"][0]["due_at"] = "2026-10-14T14:00:00Z"
    assert _validate(checkout)["id"] == "pt_at_service"
    checkout["payment"]["terms"][0]["schedules"][0]["due_at"] = "2026-10-14T15:00:00Z"
    with pytest.raises(UcpProfileValidationError, match="slot start"):
        _validate(checkout)


def _mutate(path_fn):
    checkout = copy.deepcopy(_checkout())
    path_fn(checkout)
    return checkout


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda c: c["settlement"].update(mode="mailed_check"), "not supported"),
        (lambda c: c["settlement"].update(term_id="pt_other"), "exactly one"),
        (lambda c: c["settlement"].pop("term_id"), "missing term_id"),
        (lambda c: c.pop("payment"), "requires payment.terms"),
        (lambda c: c["payment"].pop("selected_term_id"), "requires payment.terms"),
        (
            lambda c: c["payment"]["terms"][0]["schedules"][0].update(type="immediate"),
            "exactly one pay-at-service term",
        ),
        (
            lambda c: c["payment"]["terms"][0]["schedules"][0].pop("due_at"),
            "due_at",
        ),
        (
            lambda c: c["payment"]["terms"][0]["schedules"][0].update(amount=100),
            "sum to the checkout total",
        ),
        (lambda c: c.update(totals=[{"type": "subtotal", "amount": 7500}]), "total"),
    ],
)
def test_malformed_offline_checkout_fails_closed(mutation, message):
    with pytest.raises(UcpProfileValidationError, match=message):
        _validate(_mutate(mutation))


def test_two_pay_at_service_terms_are_refused():
    """S2: one offline term per checkout."""
    checkout = _checkout()
    second = copy.deepcopy(checkout["payment"]["terms"][0])
    second["id"] = "pt_second"
    checkout["payment"]["terms"].append(second)
    with pytest.raises(UcpProfileValidationError, match="exactly one pay-at-service term"):
        _validate(checkout)


def test_an_instrument_funded_term_may_sit_alongside_but_must_not_be_selected():
    """S2 allows 'pay now' beside the offline term; completing without an instrument
    is only valid when the offline term is the selected one (S4)."""
    checkout = _checkout()
    checkout["payment"]["terms"].append(
        {
            "id": "pt_now",
            "title": "Pay now",
            "schedules": [{"id": "s", "type": "immediate", "amount": 7500}],
        }
    )
    assert _validate(checkout)["id"] == "pt_at_service"
    checkout["payment"]["selected_term_id"] = "pt_now"
    with pytest.raises(UcpProfileValidationError, match="not the selected"):
        _validate(checkout)


@pytest.mark.parametrize("missing", list(_CAPS))
def test_settlement_without_a_negotiated_capability_is_refused(missing):
    caps = {k: v for k, v in _CAPS.items() if k != missing}
    if missing == "dev.ucp.shopping.checkout":
        # checkout itself is not consulted here; only the two extensions are.
        assert _validate(_checkout(), negotiated_capabilities=caps)["id"] == "pt_at_service"
        return
    with pytest.raises(UcpProfileValidationError, match="negotiated capabilities"):
        _validate(_checkout(), negotiated_capabilities=caps)


def test_a_changed_term_since_quote_is_refused():
    quoted = copy.deepcopy(_checkout()["payment"]["terms"][0])
    quoted["schedules"][0]["amount"] = 7000
    with pytest.raises(UcpProfileValidationError, match="changed since it was quoted"):
        _validate(_checkout(), persisted_term=quoted)


def test_settlement_on_a_checkout_not_routed_for_it_fails_closed():
    with pytest.raises(UcpProfileValidationError, match="not routed for"):
        _validate(_checkout(), expect_offline_settlement=False)


def test_a_pay_at_service_route_without_settlement_fails_closed():
    checkout = _checkout()
    del checkout["settlement"]
    with pytest.raises(UcpProfileValidationError, match="no settlement"):
        _validate(checkout)


def test_ordinary_checkout_is_unaffected():
    checkout = {
        "id": "c1",
        "totals": [{"type": "total", "amount": 100}],
        "payment": {"instruments": []},
    }
    assert validate_payment_terms(checkout) is None


# ---- routing -------------------------------------------------------------

_AT_SERVICE = {"policies": {"requires_payment": True, "payment_timing": "at_service"}}


def test_at_service_routes_through_checkout_only_when_declared():
    declared = classify_payment_route(_AT_SERVICE, pay_at_service_declared=True)
    assert declared == {"route": "pay_at_service", "timing": "at_service", "refusal": None}
    undeclared = classify_payment_route(_AT_SERVICE)
    assert undeclared["route"] == "at_service"


@pytest.mark.parametrize("timing", ["at_booking", "deposit_required"])
def test_declaring_the_extension_does_not_change_other_timings(timing):
    service = {"policies": {"requires_payment": True, "payment_timing": timing}}
    assert classify_payment_route(service, pay_at_service_declared=True)["route"] == timing


def test_free_service_stays_free_even_when_declared():
    service = {"policies": {"requires_payment": False}}
    assert classify_payment_route(service, pay_at_service_declared=True)["route"] == "free"


def test_timing_change_after_quote_is_still_caught_on_the_checkout_route():
    class _Session:
        payment_timing = "at_service"
        payment_route = "pay_at_service"

    assert payment_timing_changed_refusal(_Session(), _AT_SERVICE) is None
    changed = {"policies": {"requires_payment": True, "payment_timing": "at_booking"}}
    assert payment_timing_changed_refusal(_Session(), changed)["code"] == "payment_timing_changed"


# ---- business capability detection ---------------------------------------

def _profile(*caps):
    return {
        "ucp": {
            "version": "2026-08-25",
            "capabilities": {c: [{"version": "2026-08-20"}] for c in caps},
            "services": {
                "dev.ucp.shopping": [{"version": "2026-08-25", "transport": "rest", "endpoint": "https://b.example/ucp"}],
                "dev.usp-protocol.services": [
                    {"version": "2026-08-20", "transport": "rest", "endpoint": "https://b.example/usp"}
                ],
            },
        }
    }


_TERMS = "dev.ucp.common.payment.terms"
_PAS = "dev.usp-protocol.services.pay_at_service"
_CHECKOUT = "dev.ucp.shopping.checkout"
_CATALOG = "dev.usp-protocol.services.catalog"


@pytest.mark.parametrize(
    ("caps", "declared"),
    [
        ((_CATALOG, _CHECKOUT, _TERMS, _PAS), True),
        # 11.3.2: pay_at_service without the terms extension is malformed.
        ((_CATALOG, _CHECKOUT, _PAS), False),
        ((_CATALOG, _CHECKOUT, _TERMS), False),
        ((_CATALOG, _TERMS, _PAS), False),
    ],
)
def test_business_declares_pay_at_service_only_with_its_prerequisites(caps, declared):
    uctx = parse_ucp_native_context_from_document(_profile(*caps), require_paid_bookings=False)
    assert uctx.declares_pay_at_service() is declared
