"""Tests for the UCP capability seam that binds negotiation to checkout (GH-123).

The platform-side intersection is a pre-flight gate; the checkout response echo is
authoritative. These tests pin the defer-to-response semantics: the business may
narrow the active set, but activating a capability/version the platform cannot
support fails fast.
"""
import pytest

from booking_agent.ucp_capabilities import (
    CAPABILITY_HANDLERS,
    capabilities_from_checkout_response,
    validate_checkout_capabilities,
    version_from_checkout_response,
)
from booking_agent.ucp_profile import CAP_PAID_BOOKINGS, UcpProfileValidationError

CHECKOUT_CAP = "dev.ucp.shopping.checkout"


def _checkout_with_caps(caps: dict, *, version: str = "2026-01-11") -> dict:
    return {"id": "cs_1", "ucp": {"version": version, "capabilities": caps}}


def _platform_supported() -> dict:
    return {CHECKOUT_CAP: ["2026-01-11"], CAP_PAID_BOOKINGS: ["1.0"]}


def test_extracts_profile_form_echo():
    checkout = _checkout_with_caps({CAP_PAID_BOOKINGS: [{"version": "1.0"}]})
    assert capabilities_from_checkout_response(checkout) == {CAP_PAID_BOOKINGS: ["1.0"]}
    assert version_from_checkout_response(checkout) == "2026-01-11"


def test_extracts_flattened_form_echo():
    checkout = _checkout_with_caps({CAP_PAID_BOOKINGS: ["1.0"]})
    assert capabilities_from_checkout_response(checkout) == {CAP_PAID_BOOKINGS: ["1.0"]}


def test_echo_equal_to_negotiated_passes():
    checkout = _checkout_with_caps(
        {CHECKOUT_CAP: ["2026-01-11"], CAP_PAID_BOOKINGS: ["1.0"]}
    )
    result = validate_checkout_capabilities(checkout, _platform_supported())
    assert result == {CHECKOUT_CAP: ["2026-01-11"], CAP_PAID_BOOKINGS: ["1.0"]}


def test_business_narrowing_is_accepted_and_echo_is_returned():
    """Business narrows the set by cart context; platform must defer, not error.

    The returned (authoritative) set is the *echo*, not the broader platform set.
    """
    checkout = _checkout_with_caps({CAP_PAID_BOOKINGS: ["1.0"]})  # checkout cap dropped
    result = validate_checkout_capabilities(checkout, _platform_supported())
    assert result == {CAP_PAID_BOOKINGS: ["1.0"]}
    assert CHECKOUT_CAP not in result


def test_unsupported_version_activation_fails_fast():
    checkout = _checkout_with_caps({CAP_PAID_BOOKINGS: ["2.0"]})  # platform only has 1.0
    with pytest.raises(UcpProfileValidationError, match="does not support"):
        validate_checkout_capabilities(checkout, _platform_supported())


def test_unsupported_namespace_activation_fails_fast():
    checkout = _checkout_with_caps({"dev.vendor.unknown": ["1.0"]})
    with pytest.raises(UcpProfileValidationError, match="unsupported capability"):
        validate_checkout_capabilities(checkout, _platform_supported())


def test_all_unsupported_activations_are_named_in_one_error():
    """Production HVAC-Koby 201 echoes a static shopping map, not the intersection.

    A first-only raise would hide catalog.search after a fulfillment-only merchant
    patch. The platform advertises checkout + paid_bookings only.
    """
    fulfillment = "dev.ucp.shopping.fulfillment"
    catalog_search = "dev.ucp.shopping.catalog.search"
    catalog_lookup = "dev.ucp.shopping.catalog.lookup"
    checkout = _checkout_with_caps(
        {
            CHECKOUT_CAP: ["2026-04-08"],
            fulfillment: ["2026-04-08"],
            catalog_search: ["2026-04-08"],
            catalog_lookup: ["2026-04-08"],
        }
    )
    platform = {
        CHECKOUT_CAP: ["2026-04-08"],
        CAP_PAID_BOOKINGS: ["2026-08-20"],
    }
    with pytest.raises(UcpProfileValidationError) as exc_info:
        validate_checkout_capabilities(checkout, platform)
    message = str(exc_info.value)
    assert fulfillment in message
    assert catalog_search in message
    assert catalog_lookup in message
    assert "2026-04-08" in message
    assert CAP_PAID_BOOKINGS in message or "supported" in message


def test_unsupported_namespace_and_version_are_reported_together():
    checkout = _checkout_with_caps(
        {
            CAP_PAID_BOOKINGS: ["2.0"],
            "dev.ucp.shopping.fulfillment": ["2026-04-08"],
        }
    )
    with pytest.raises(UcpProfileValidationError) as exc_info:
        validate_checkout_capabilities(checkout, _platform_supported())
    message = str(exc_info.value)
    assert "dev.ucp.shopping.fulfillment" in message
    assert "2.0" in message
    assert CAP_PAID_BOOKINGS in message
    assert "does not support" in message or "1.0" in message


def test_empty_echo_returns_empty_without_error():
    checkout = _checkout_with_caps({})
    assert validate_checkout_capabilities(checkout, _platform_supported()) == {}


def test_pruned_extension_is_enforced_on_checkout():
    """AC linkage: intersection prunes an extension, and the checkout is bound to it.

    Platform lacks the checkout parent, so paid_bookings is pruned from the
    negotiated set. A checkout response that nonetheless activates paid_bookings
    must fail fast (the negotiated set is enforced, not merely computed).
    """
    from booking_agent.ucp_negotiation import intersect_ucp_capabilities

    plat = {"ucp": {"capabilities": {"dev.usp-protocol.services.catalog": [{"version": "1.0"}]}}}
    bus = {
        "ucp": {
            "capabilities": {
                CHECKOUT_CAP: [{"version": "2026-01-11"}],
                CAP_PAID_BOOKINGS: [{"version": "1.0", "extends": CHECKOUT_CAP}],
                "dev.usp-protocol.services.catalog": [{"version": "1.0"}],
            }
        }
    }
    platform_supported = intersect_ucp_capabilities(plat, bus)
    assert CAP_PAID_BOOKINGS not in platform_supported

    checkout = _checkout_with_caps({CAP_PAID_BOOKINGS: ["1.0"]})
    with pytest.raises(UcpProfileValidationError):
        validate_checkout_capabilities(checkout, platform_supported)


def test_registered_handler_runs_and_unknown_registered_namespace_defers():
    """Seam extensibility: a second registered handler runs; proves AP2 plugs in.

    Register a dummy capability handler, assert it is invoked for its namespace and
    that unregistered-but-supported namespaces fall back to the generic check.
    """
    calls = []

    def _dummy(echoed, supported):
        calls.append((tuple(echoed), tuple(supported)))

    ns = "dev.vendor.ap2_like"
    CAPABILITY_HANDLERS[ns] = _dummy
    try:
        checkout = _checkout_with_caps({ns: ["0.1"], CAP_PAID_BOOKINGS: ["1.0"]})
        supported = {**_platform_supported(), ns: ["0.1"]}
        validate_checkout_capabilities(checkout, supported)
        assert calls == [(("0.1",), ("0.1",))]
    finally:
        del CAPABILITY_HANDLERS[ns]
