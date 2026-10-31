"""Headless USP orchestration helpers.

The booking-agent-cli flow engine and the low-level
``booking-agent`` subcommands both call into these.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _parse_iso(s: str) -> datetime:
    """Parse an ISO8601 datetime string, handling the Z suffix for Python 3.10."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

from .backend import USPBackend
from .discovery import USPBusinessProfile
from .models.platform import (
    DesiredServiceTimeFrom,
    DesiredServiceTimeMoment,
    DesiredServiceTimePreference,
    DesiredServiceTimeRange,
    ServiceSearchIntent,
    ServiceSearchResult,
)
from .models.wire import (
    AvailabilityResult,
    Booking,
    CancellationPolicy,
    Channel,
    ChannelType,
    Duration,
    Pricing,
    PricingModel,
    ReschedulingPolicy,
    ServiceCategory,
    ServicePolicies,
    TimeSlot,
    USPService,
    project_category_string,
)

logger = logging.getLogger(__name__)

CATALOG_SOURCE_REGISTRY = "registry"
CATALOG_SOURCE_LIVE = "live"
METADATA_CATALOG_SOURCE = "catalog_source"
METADATA_PROFILE_RESOLVED = "profile_resolved"
METADATA_REGISTRY_SEARCH_RESULT = "registry_search_result"
METADATA_SEARCH_RANK = "search_rank"


def build_desired_service_time_preferences(
    *,
    desired_at: Optional[List[str]] = None,
    desired_from: Optional[List[str]] = None,
    desired_range: Optional[List[str]] = None,
) -> List[DesiredServiceTimePreference]:
    """Parse CLI ranking preferences and require timezone-aware RFC 3339 values."""

    def valid_rfc3339(raw: str) -> str:
        try:
            parsed = _parse_iso(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid RFC3339 timestamp: {raw!r}") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"RFC3339 timestamp must include a timezone: {raw!r}")
        return raw

    preferences: List[DesiredServiceTimePreference] = [
        DesiredServiceTimeMoment(at=valid_rfc3339(value))
        for value in desired_at or []
    ]
    preferences.extend(
        DesiredServiceTimeFrom(start=valid_rfc3339(value))
        for value in desired_from or []
    )
    for value in desired_range or []:
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 2 or not all(parts):
            raise ValueError(
                "Desired range must be <startRfc3339>,<endRfc3339>"
            )
        start, end = (valid_rfc3339(part) for part in parts)
        if _parse_iso(end) < _parse_iso(start):
            raise ValueError("Desired range end must not be before start")
        preferences.append(DesiredServiceTimeRange(start=start, end=end))
    return preferences


def attach_registry_hit(
    service: USPService,
    hit: ServiceSearchResult,
    *,
    rank: Optional[int] = None,
) -> USPService:
    """Persist the raw registry row on a discoverable service for agents and support reports."""
    meta = dict(service.metadata or {})
    meta[METADATA_REGISTRY_SEARCH_RESULT] = hit.model_dump(by_alias=True, exclude_none=True)
    if rank is not None:
        meta[METADATA_SEARCH_RANK] = rank
    service.metadata = meta
    return service


def _duration_fixed_from_minutes(minutes: Optional[int]) -> str:
    if minutes is None or minutes <= 0:
        return "PT1H"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"PT{hours}H"
    return f"PT{minutes}M"


def _pricing_from_registry_hit(hit: ServiceSearchResult) -> Pricing:
    """Map registry search pricing onto a catalog ``Pricing`` snapshot."""
    registry_price = hit.price
    currency = (
        registry_price.currency
        if registry_price and registry_price.currency
        else "USD"
    )
    pricing_model = PricingModel.VARIABLE
    if registry_price and registry_price.model:
        try:
            pricing_model = PricingModel(registry_price.model)
        except ValueError:
            pricing_model = PricingModel.VARIABLE

    if registry_price and registry_price.amount is not None:
        if pricing_model is PricingModel.VARIABLE:
            pricing_model = PricingModel.FIXED
        return Pricing(
            model=pricing_model,
            amount=registry_price.amount,
            currency=currency,
        )

    if pricing_model is PricingModel.FREE:
        return Pricing(model=PricingModel.FREE, currency=currency)

    return Pricing(model=PricingModel.VARIABLE, currency=currency)


def service_from_registry_hit(hit: ServiceSearchResult) -> USPService:
    """Build a discoverable ``USPService`` snapshot from a registry search hit.

    Used when profile resolution or live catalog fetch fails so agents can still
    present registry-ranked options to the user.
    """
    if not hit.business:
        raise ValueError("Registry search hit is missing business info")
    pricing = _pricing_from_registry_hit(hit)
    categories = (
        [ServiceCategory(taxonomy="merchant", name=hit.category)]
        if hit.category
        else None
    )
    return USPService(
        id=hit.service_id,
        business_id=hit.business.id,
        name=hit.service_name,
        type="appointment",
        categories=categories,
        duration=Duration(fixed=_duration_fixed_from_minutes(hit.duration_minutes)),
        pricing=pricing,
        channel=Channel(type=ChannelType.AT_BUSINESS_LOCATION),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            confirmation_mode="auto",
            requires_payment=False,
        ),
        business=hit.business,
        availability_hint=hit.availability_hint,
        metadata={
            METADATA_CATALOG_SOURCE: CATALOG_SOURCE_REGISTRY,
            METADATA_PROFILE_RESOLVED: False,
        },
    )


def mark_live_catalog_service(service: USPService) -> USPService:
    """Tag a live-catalog service so discover output can distinguish sources."""
    meta = dict(service.metadata or {})
    meta[METADATA_CATALOG_SOURCE] = CATALOG_SOURCE_LIVE
    meta[METADATA_PROFILE_RESOLVED] = True
    service.metadata = meta
    return service


def parse_service_search_location(location: str) -> "ServiceSearchLocation":
    """Parse ``lat,lng`` into a registry ``ServiceSearchLocation`` (coordinates only)."""
    from .models.platform import ServiceSearchLocation
    from .models.wire import Coordinates

    raw = (location or "").strip()
    parts = [p.strip() for p in raw.split(",", 1)]
    if len(parts) != 2:
        raise ValueError(
            "Location must be latitude,longitude (e.g. 32.6927,-114.6277)"
        )
    try:
        lat = float(parts[0])
        lng = float(parts[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Location must be numeric latitude,longitude (e.g. 32.6927,-114.6277)"
        ) from exc
    return ServiceSearchLocation(coordinates=Coordinates(lat=lat, lng=lng))


# USP §1.3.1 core verticals. The registry filters service search hits by exact
# match on each service's single ``type``, so an unlisted value matches nothing.
CORE_SERVICE_VERTICALS = (
    "appointment",
    "group",
    "reservation",
    "rental",
    "field_service",
)
_CUSTOM_VERTICAL_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9_-]+){2,}$")


def parse_service_verticals(values: Optional[List[str]]) -> List[str]:
    """Validate ``--vertical`` values: USP core verticals or reverse-domain custom ones."""
    verticals: List[str] = []
    for raw in values or []:
        value = (raw or "").strip()
        if not value:
            continue
        if value not in CORE_SERVICE_VERTICALS and not _CUSTOM_VERTICAL_RE.match(value):
            raise ValueError(
                f"Unknown vertical {value!r}. Use one of "
                f"{', '.join(CORE_SERVICE_VERTICALS)}, or a reverse-domain "
                "custom vertical such as com.vendor.services.name."
            )
        if value not in verticals:
            verticals.append(value)
    return verticals


def build_search_intent(
    *,
    query: str,
    natural_language_description: Optional[str] = None,
    location: Optional[Any] = None,
    verticals: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    date_preference: Optional[str] = None,
    desired_service_time_ranges: Optional[List[DesiredServiceTimePreference]] = None,
    prefer_sooner_availability_slots: Optional[bool] = None,
    intent_expiry_minutes: int = 30,
) -> ServiceSearchIntent:
    """Build a ServiceSearchIntent with sane defaults."""
    expiry = (
        datetime.now(timezone.utc) + timedelta(minutes=intent_expiry_minutes)
    ).isoformat()
    loc = location
    if isinstance(location, dict):
        from .models.platform import ServiceSearchLocation

        loc = ServiceSearchLocation(**location)
    return ServiceSearchIntent(
        query=query,
        natural_language_description=natural_language_description or query,
        location=loc,
        verticals=verticals or [],
        categories=categories or [],
        date_preference=date_preference,
        desired_service_time_ranges=desired_service_time_ranges,
        prefer_sooner_availability_slots=prefer_sooner_availability_slots,
        intent_expiry=expiry,
    )


async def discover_service_via_registry(
    backend: USPBackend,
    *,
    query: str,
    location: Optional[Any] = None,
    verticals: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
) -> Tuple[str, str, ServiceSearchResult]:
    """Return ``(service_id, profile_url, raw_hit)`` from registry search (GH-021).

    Does not post-filter by ``deployment_mode`` (per USP+UCP implementation plan).
    """
    from .models.platform import ServiceSearchResult, ServiceSearchResultPrice
    from .models.wire import RegistryBusinessInfo

    intent = build_search_intent(
        query=query,
        location=location,
        verticals=verticals,
        categories=categories,
    )
    services = await backend.search_registry(intent)
    for s in services:
        biz = s.business
        if not s.id or not biz or not biz.usp_profile_url:
            continue
        hit = ServiceSearchResult(
            service_id=s.id,
            service_name=s.name or "",
            business=RegistryBusinessInfo(
                id=biz.id,
                name=biz.name,
                profile_url=biz.usp_profile_url,
                deployment_mode=biz.deployment_mode or "standalone",
            ),
            category=project_category_string(
                categories=s.categories,
                service_type=s.type,
            ),
            price=(
                ServiceSearchResultPrice(
                    amount=s.pricing.amount if s.pricing else None,
                    currency=s.pricing.currency if s.pricing else None,
                )
                if s.pricing
                else None
            ),
            timezone=None,
        )
        return s.id, biz.usp_profile_url, hit
    raise RuntimeError("No registry search hit with service_id and business.profile_url")


async def discover_services(
    backend: USPBackend, intent: ServiceSearchIntent
) -> List[USPService]:
    """Search the USP registry."""
    return await backend.search_registry(intent)


async def resolve_profile(
    backend: USPBackend, usp_profile_url: str
) -> USPBusinessProfile:
    return await backend.resolve_profile(usp_profile_url)


def _isoformat_day_bounds(day: str) -> Tuple[str, str]:
    """Return ``(start, end)`` ISO8601 UTC bounds for an ISO date ``YYYY-MM-DD``."""
    d = datetime.fromisoformat(day).date()
    start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


async def query_availability_with_quirk(
    backend: USPBackend,
    profile: USPBusinessProfile,
    service_id: str,
    start_date: str,
    end_date: str,
    *,
    timezone_str: Optional[str] = None,
    single_day_retry_days: int = 7,
    **booking_questions: Any,
) -> List[TimeSlot]:
    """Query availability, widening an empty single-day window once.

    If the requested window is a single day and returns 0 slots, retry with a
    ``single_day_retry_days``-wide range starting at the same date and keep only
    the slots that fall on that date.
    """
    slots = await backend.query_availability(
        profile, service_id, start_date, end_date, timezone_str, **booking_questions
    )
    if slots:
        return slots

    try:
        start_dt = _parse_iso(start_date)
        end_dt = _parse_iso(end_date)
    except (ValueError, TypeError):
        return slots

    if (end_dt.date() - start_dt.date()).days > 0:
        return slots

    target_date = start_dt.date()
    date_only = _is_date_only(start_date) and _is_date_only(end_date)
    if date_only:
        retry_end = (target_date + timedelta(days=single_day_retry_days - 1)).isoformat()
    else:
        window_tz = start_dt.tzinfo or timezone.utc
        retry_end = (
            datetime(target_date.year, target_date.month, target_date.day, 0, 0, tzinfo=window_tz)
            + timedelta(days=single_day_retry_days)
            - timedelta(seconds=1)
        ).isoformat()
    logger.info(
        "Single-day availability empty for %s on %s — retrying with %d-day range",
        service_id, target_date, single_day_retry_days,
    )
    wide = await backend.query_availability(
        profile, service_id, start_date, retry_end, timezone_str, **booking_questions
    )
    if date_only:
        applied = _valid_zone(getattr(wide, "applied_date_bounds_timezone", None))
        window_tz = ZoneInfo(applied) if applied else None

    def _on_target_day(raw: str) -> bool:
        dt = _parse_iso(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(window_tz) if window_tz is not None else dt
        return local.date() == target_date

    kept = [s for s in wide if _on_target_day(s.start)]
    return AvailabilityResult(
        kept,
        business_timezone=getattr(wide, "business_timezone", None),
        applied_date_bounds_timezone=getattr(wide, "applied_date_bounds_timezone", None),
    )


def _is_date_only(value: str) -> bool:
    return isinstance(value, str) and "T" not in value and len(value.strip()) == 10


def _valid_zone(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        ZoneInfo(value.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return value.strip()


async def create_booking(
    backend: USPBackend,
    profile: USPBusinessProfile,
    *,
    service_id: str,
    slot_id: str,
    buyer: Dict[str, Any],
    party_size: int = 1,
    resource_id: Optional[str] = None,
    notes: Optional[str] = None,
    delivery_address: Optional[Dict[str, Any]] = None,
) -> Booking:
    return await backend.create_booking(
        profile=profile,
        service_id=service_id,
        slot_id=slot_id,
        buyer=buyer,
        party_size=party_size,
        resource_id=resource_id,
        notes=notes,
        delivery_address=delivery_address,
    )


async def poll_booking(
    backend: USPBackend, profile: USPBusinessProfile, booking_id: str
) -> Booking:
    return await backend.get_booking(profile, booking_id)
