"""Which surfaces the booking questions extension is active on (Sections 8.3, 11.4.2).

The extension is multi-parent: it stays active while any parent is negotiated, and
each of its fields carries meaning only on a surface whose own parent is. An empty
result means today's flow exactly: question sets in the catalog are ignored, and no
submission, assessment token or eligibility call is sent.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional

from booking_agent.namespace import (
    CAP_AVAILABILITY,
    CAP_BOOKING_QUESTIONS,
    CAP_BOOKINGS,
    CAP_CATALOG,
    CAP_PAID_BOOKINGS,
)
from booking_agent.ucp_negotiation import _capabilities_from_doc, _extends_map_from_doc

logger = logging.getLogger(__name__)

SURFACE_PARENTS: Dict[str, str] = {
    "catalog": CAP_CATALOG,
    "availability": CAP_AVAILABILITY,
    "bookings": CAP_BOOKINGS,
    "paid_bookings": CAP_PAID_BOOKINGS,
}

INACTIVE: FrozenSet[str] = frozenset()


def _surfaces(parents: Iterable[str], negotiated: Iterable[str]) -> FrozenSet[str]:
    declared = set(parents) or set(SURFACE_PARENTS.values())
    active = set(negotiated)
    return frozenset(
        surface for surface, cap in SURFACE_PARENTS.items()
        if cap in declared and cap in active
    )


def _common(plat: Mapping[str, List[str]], bus: Mapping[str, List[str]]) -> List[str]:
    common = []
    for cap_id, versions in bus.items():
        if cap_id not in plat:
            continue
        plat_versions = plat[cap_id]
        if not plat_versions or set(versions) & set(plat_versions):
            common.append(cap_id)
    return common


def standalone_active_surfaces(
    business_usp: Mapping[str, Any], platform_doc: Mapping[str, Any],
) -> FrozenSet[str]:
    """Activation in Standalone Mode: intersect the two ``usp.capabilities`` blocks."""
    platform_usp = platform_doc.get("usp") if isinstance(platform_doc, Mapping) else None
    if not isinstance(platform_usp, Mapping):
        return INACTIVE
    plat = _capabilities_from_doc(dict(platform_usp))
    bus = _capabilities_from_doc(dict(business_usp))
    if CAP_BOOKING_QUESTIONS not in plat or CAP_BOOKING_QUESTIONS not in bus:
        return INACTIVE
    negotiated = _common(plat, bus)
    if CAP_BOOKING_QUESTIONS not in negotiated:
        return INACTIVE
    parents = _extends_map_from_doc(dict(business_usp)).get(CAP_BOOKING_QUESTIONS) or []
    return _surfaces(parents, negotiated)


def ucp_active_surfaces(
    intersection: Mapping[str, Any], business_doc: Mapping[str, Any],
) -> FrozenSet[str]:
    """Activation in UCP-Native Mode, read from ``intersect_ucp_capabilities``."""
    if CAP_BOOKING_QUESTIONS not in intersection:
        return INACTIVE
    parents = _extends_map_from_doc(dict(business_doc)).get(CAP_BOOKING_QUESTIONS) or []
    return _surfaces(parents, intersection)


def business_declares_booking_questions(business_usp: Optional[Mapping[str, Any]]) -> bool:
    caps = (business_usp or {}).get("capabilities") if isinstance(business_usp, Mapping) else None
    return isinstance(caps, Mapping) and CAP_BOOKING_QUESTIONS in caps


async def booking_questions_activation(business_usp: Optional[Mapping[str, Any]]) -> FrozenSet[str]:
    """Standalone activation for one business, fetching the platform profile only when needed.

    A business that does not declare the extension costs nothing. A failed platform
    profile fetch means inactive.
    """
    if not business_declares_booking_questions(business_usp):
        return INACTIVE
    from booking_agent.platform_profile import platform_profile_url
    from booking_agent.ucp_negotiation import load_platform_profile

    try:
        platform_doc = await load_platform_profile(platform_profile_url())
    except Exception as e:  # noqa: BLE001 - any failure leaves the extension off
        logger.info("platform profile unavailable, booking questions inactive: %s", e)
        return INACTIVE
    return standalone_active_surfaces(business_usp or {}, platform_doc)
