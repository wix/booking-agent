"""Mock USP business profiles for offline tests.

Versions and capability spec/schema URLs follow the USP spec (schemas/usp.json
ProfileCapabilityEntry: version MUST match YYYY-MM-DD; spec + schema URLs are
REQUIRED in business and platform profiles).
"""
from typing import Dict

from booking_agent.discovery import (
    USPBusinessInfo,
    USPBusinessProfile,
    USPCapabilityEntry,
    USPTransportBinding,
)
from booking_agent.models.platform import BookingPaymentMode

USP_VERSION = "2026-02-09"
_SPEC_BASE = "https://usp-protocol.dev"


def _cap(name: str) -> USPCapabilityEntry:
    """Build a ProfileCapabilityEntry for the given reverse-domain capability."""
    short = name.rsplit(".", 1)[-1]  # e.g. "catalog"
    return USPCapabilityEntry(
        version=USP_VERSION,
        spec=f"{_SPEC_BASE}/spec/{short}",
        schema_url=f"{_SPEC_BASE}/schemas/services/{short}.json",
    )


def mock_profile(name: str, endpoint: str) -> USPBusinessProfile:
    return USPBusinessProfile(
        version=USP_VERSION,
        services={
            "dev.usp-protocol.services": [
                USPTransportBinding(version=USP_VERSION, transport="rest", endpoint=endpoint)
            ]
        },
        capabilities={
            "dev.usp-protocol.services.catalog": [_cap("dev.usp-protocol.services.catalog")],
            "dev.usp-protocol.services.availability": [_cap("dev.usp-protocol.services.availability")],
            "dev.usp-protocol.services.bookings": [_cap("dev.usp-protocol.services.bookings")],
        },
        checkout_systems=[BookingPaymentMode.REDIRECT.value],
        business=USPBusinessInfo(name=name, timezone="America/Phoenix", currency="USD"),
    )


MOCK_PROFILES: Dict[str, USPBusinessProfile] = {
    "desert-sun-barbershop.example.com": mock_profile(
        "Desert Sun Barbershop",
        "https://desert-sun-barbershop.example.com/usp/v1",
    ),
    "kobym9.wixsite.com": mock_profile(
        "Koby's HVAC Services",
        "https://kobym9.wixsite.com/hvac-koby",
    ),
}
