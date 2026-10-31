import pytest

from booking_agent.ucp_negotiation import intersect_ucp_capabilities, negotiate_ucp_protocol_version
from booking_agent.ucp_profile import UcpProfileValidationError


# ---------------------------------------------------------------------------
# negotiate_ucp_protocol_version
# ---------------------------------------------------------------------------

def test_negotiate_falls_back_when_platform_has_no_version():
    plat = {"ucp": {}}
    bus = {"ucp": {"version": "2026-01-11"}}
    assert negotiate_ucp_protocol_version(plat, bus) == "2026-01-11"


def test_negotiate_intersection():
    plat = {"ucp": {"version": "2026-01-11"}}
    bus = {"ucp": {"version": "2026-01-11"}}
    assert negotiate_ucp_protocol_version(plat, bus) == "2026-01-11"


def test_negotiate_version_mismatch_raises():
    """Platform and business declare non-overlapping versions - must raise."""
    plat = {"ucp": {"version": "2025-01-01"}}
    bus = {"ucp": {"version": "2026-01-11"}}
    with pytest.raises(UcpProfileValidationError, match="overlapping"):
        negotiate_ucp_protocol_version(plat, bus)


def test_negotiate_raises_when_business_has_no_version():
    plat = {"ucp": {"version": "2026-01-11"}}
    bus = {"ucp": {}}
    with pytest.raises(UcpProfileValidationError, match="ucp.version"):
        negotiate_ucp_protocol_version(plat, bus)


# ---------------------------------------------------------------------------
# intersect_ucp_capabilities
# ---------------------------------------------------------------------------

def _bus_doc_with_paid_bookings() -> dict:
    return {
        "ucp": {
            "capabilities": {
                "dev.ucp.shopping.checkout": [{"version": "2026-01-11"}],
                "dev.usp-protocol.services.paid_bookings": [
                    {"version": "1.0", "extends": "dev.ucp.shopping.checkout"}
                ],
                "dev.usp-protocol.services.catalog": [{"version": "1.0"}],
            }
        }
    }


def _plat_doc_matching_bus(*cap_ids: str) -> dict:
    """Build a platform profile whose capability versions match _bus_doc_with_paid_bookings."""
    version_map = {
        "dev.ucp.shopping.checkout": "2026-01-11",
        "dev.usp-protocol.services.paid_bookings": "1.0",
        "dev.usp-protocol.services.catalog": "1.0",
    }
    return {
        "ucp": {
            "capabilities": {
                cap_id: [{"version": version_map.get(cap_id, "1.0")}]
                for cap_id in cap_ids
            }
        }
    }


def test_intersect_returns_common_capabilities():
    plat = _plat_doc_matching_bus("dev.ucp.shopping.checkout", "dev.usp-protocol.services.paid_bookings")
    bus = _bus_doc_with_paid_bookings()
    result = intersect_ucp_capabilities(plat, bus)
    assert "dev.ucp.shopping.checkout" in result
    assert "dev.usp-protocol.services.paid_bookings" in result
    # catalog not declared by platform - should be pruned
    assert "dev.usp-protocol.services.catalog" not in result


def test_intersect_prunes_extension_when_parent_pruned():
    """paid_bookings extends checkout; when checkout is absent from platform, paid_bookings is pruned.

    catalog has no extends so it survives; the intersection is non-empty.
    """
    plat = _plat_doc_matching_bus("dev.usp-protocol.services.catalog")  # supports catalog but NOT checkout
    bus = _bus_doc_with_paid_bookings()
    result = intersect_ucp_capabilities(plat, bus)
    # paid_bookings depends on checkout which platform doesn't support - must be pruned
    assert "dev.usp-protocol.services.paid_bookings" not in result
    # catalog has no extends and is in platform - must survive
    assert "dev.usp-protocol.services.catalog" in result
    # checkout itself is not in platform
    assert "dev.ucp.shopping.checkout" not in result


def test_intersect_raises_when_no_overlap():
    plat = _plat_doc_matching_bus("dev.ucp.shopping.checkout")
    bus = {"ucp": {"capabilities": {"dev.usp-protocol.services.catalog": [{"version": "1.0"}]}}}
    with pytest.raises(UcpProfileValidationError, match="intersection is empty"):
        intersect_ucp_capabilities(plat, bus)


def test_intersect_passthrough_when_platform_has_no_capabilities():
    """A platform profile without a capabilities block should not block any business capability."""
    plat = {"ucp": {}}
    bus = _bus_doc_with_paid_bookings()
    result = intersect_ucp_capabilities(plat, bus)
    assert "dev.ucp.shopping.checkout" in result
    assert "dev.usp-protocol.services.paid_bookings" in result
    assert "dev.usp-protocol.services.catalog" in result


def test_intersect_prunes_non_matching_versions():
    """When platform and business share a capability but not a common version, prune it."""
    plat = {"ucp": {"capabilities": {
        "dev.ucp.shopping.checkout": [{"version": "2025-01-01"}],  # old version only
    }}}
    bus = {"ucp": {"capabilities": {
        "dev.ucp.shopping.checkout": [{"version": "2026-01-11"}],  # different version
        "dev.usp-protocol.services.paid_bookings": [
            {"version": "1.0", "extends": "dev.ucp.shopping.checkout"}
        ],
    }}}
    with pytest.raises(UcpProfileValidationError, match="intersection is empty"):
        intersect_ucp_capabilities(plat, bus)


def test_negotiate_accepts_business_2026_04_08_when_platform_lists_both():
    plat = {
        "ucp": {
            "version": "2026-08-25",
            "supported_versions": ["2026-04-08", "2026-08-25"],
        }
    }
    bus = {"ucp": {"version": "2026-04-08"}}
    assert negotiate_ucp_protocol_version(plat, bus) == "2026-04-08"


def _payment_terms_docs(*, platform_order: bool, business_order: bool):
    plat_caps = {
        "dev.ucp.shopping.checkout": [{"version": "2026-08-25"}],
        "dev.ucp.common.payment.terms": [{"version": "2026-08-25"}],
    }
    bus_caps = {
        "dev.ucp.shopping.checkout": [{"version": "2026-08-25"}],
        "dev.ucp.common.payment.terms": [
            {
                "version": "2026-08-25",
                "extends": ["dev.ucp.shopping.checkout", "dev.ucp.shopping.order"],
            }
        ],
    }
    if platform_order:
        plat_caps["dev.ucp.shopping.order"] = [{"version": "2026-08-25"}]
    if business_order:
        bus_caps["dev.ucp.shopping.order"] = [{"version": "2026-08-25"}]
    return {"ucp": {"capabilities": plat_caps}}, {"ucp": {"capabilities": bus_caps}}


def test_intersect_keeps_multi_parent_extension_when_one_parent_present():
    plat, bus = _payment_terms_docs(platform_order=False, business_order=False)
    result = intersect_ucp_capabilities(plat, bus)
    assert "dev.ucp.shopping.checkout" in result
    assert "dev.ucp.common.payment.terms" in result


def test_intersect_keeps_payment_terms_with_both_parents():
    plat, bus = _payment_terms_docs(platform_order=True, business_order=True)
    result = intersect_ucp_capabilities(plat, bus)
    assert {"dev.ucp.shopping.checkout", "dev.ucp.shopping.order",
            "dev.ucp.common.payment.terms"} <= set(result)


def test_intersect_prunes_multi_parent_extension_when_no_parent_present():
    plat = {"ucp": {"capabilities": {
        "dev.ucp.shopping.checkout": [{"version": "2026-08-25"}],
        "dev.usp-protocol.services.catalog": [{"version": "2026-08-20"}],
        "dev.ucp.common.payment.terms": [{"version": "2026-08-25"}],
    }}}
    bus = {"ucp": {"capabilities": {
        "dev.usp-protocol.services.catalog": [{"version": "2026-08-20"}],
        "dev.ucp.common.payment.terms": [{
            "version": "2026-08-25",
            "extends": ["dev.ucp.shopping.checkout", "dev.ucp.shopping.order"],
        }],
    }}}
    result = intersect_ucp_capabilities(plat, bus)
    assert "dev.ucp.common.payment.terms" not in result


def test_intersect_booking_questions_active_for_checkout_only():
    """Mirrors bq-flow-ucp-multi-parent-pruning: only paid_bookings is shared."""
    bq = "dev.usp-protocol.services.booking_questions"
    plat = {"ucp": {"capabilities": {
        "dev.ucp.shopping.checkout": [{"version": "2026-08-25"}],
        "dev.usp-protocol.services.paid_bookings": [{"version": "2026-08-20"}],
        bq: [{"version": "2026-08-20"}],
    }}}
    bus = {"ucp": {"capabilities": {
        "dev.ucp.shopping.checkout": [{"version": "2026-08-25"}],
        "dev.usp-protocol.services.paid_bookings": [
            {"version": "2026-08-20", "extends": "dev.ucp.shopping.checkout"},
        ],
        "dev.usp-protocol.services.catalog": [{"version": "2026-08-20"}],
        bq: [{
            "version": "2026-08-20",
            "extends": [
                "dev.usp-protocol.services.catalog",
                "dev.usp-protocol.services.paid_bookings",
            ],
        }],
    }}}
    result = intersect_ucp_capabilities(plat, bus)
    assert bq in result
    assert "dev.usp-protocol.services.catalog" not in result

    from booking_agent.booking_questions_negotiation import ucp_active_surfaces

    surfaces = ucp_active_surfaces(result, bus)
    assert surfaces == {"paid_bookings"}
