"""Business and buyer timezone helpers for slot presentation.

The business timezone is authoritative for availability and every primary slot
display. The buyer timezone comes only from the default Link shipping address:
geocoded over HTTPS, then mapped to an IANA zone locally with ``timezonefinder``.
There is no device, host, process, VPN, or cloud-region fallback.
"""
from __future__ import annotations

import atexit
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from booking_agent_cli.geocode import GeocodeError, geocode_address

BUYER_TIMEZONE_SOURCE = "link_shipping_address"
# Geocoder and timezone failures share one sanitized error type.
BuyerTimezoneError = GeocodeError


@dataclass
class BuyerTimezoneResult:
    timezone: Optional[str] = None
    source: Optional[str] = None
    error: Optional[str] = None


def valid_iana_timezone(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def _parse_instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def zone_label(tz_name: str) -> str:
    """``America/Phoenix`` -> ``Phoenix time (America/Phoenix)``."""
    city = tz_name.rsplit("/", 1)[-1].replace("_", " ")
    if "/" not in tz_name or not city.isascii() or not city.replace(" ", "").isalpha():
        return f"{tz_name} time"
    return f"{city} time ({tz_name})"


def format_instant(value: Any, tz_name: Optional[str]) -> Optional[str]:
    """``Tue, Oct 6, 2026, 7:00 AM MST (America/Phoenix)``, or None if unusable.

    With no zone the instant keeps its own offset:
    ``Tue, Oct 6, 2026, 2:00 PM (UTC-07:00)``. A string without an offset has
    no usable time then, so it returns None.
    """
    if tz_name is None:
        return _format_own_offset(value)
    dt = _parse_instant(value)
    zone = valid_iana_timezone(tz_name)
    if dt is None or zone is None:
        return None
    local = dt.astimezone(ZoneInfo(zone))
    hour = local.hour % 12 or 12
    return (
        f"{local:%a, %b} {local.day}, {local.year}, {hour}:{local:%M} {local:%p} "
        f"{local.tzname()} ({zone})"
    )


def _format_own_offset(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    offset = dt.utcoffset()
    if offset is None:
        return None
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    hh, mm = divmod(abs(minutes), 60)
    hour = dt.hour % 12 or 12
    return (
        f"{dt:%a, %b} {dt.day}, {dt.year}, {hour}:{dt:%M} {dt:%p} "
        f"(UTC{sign}{hh:02d}:{mm:02d})"
    )


def zones_differ(business_tz: Optional[str], buyer_tz: Optional[str], instants: Iterable[Any]) -> bool:
    """True when both zones are valid and their UTC offsets differ at any given instant."""
    biz = valid_iana_timezone(business_tz)
    buyer = valid_iana_timezone(buyer_tz)
    if biz is None or buyer is None or biz == buyer:
        return False
    points = [dt for dt in (_parse_instant(v) for v in instants) if dt is not None]
    if not points:
        points = [datetime.now(timezone.utc)]
    biz_zone, buyer_zone = ZoneInfo(biz), ZoneInfo(buyer)
    return any(
        dt.astimezone(biz_zone).utcoffset() != dt.astimezone(buyer_zone).utcoffset()
        for dt in points
    )


def timezone_notice(business_tz: str, buyer_tz: str) -> str:
    return (
        "I'm detecting a timezone difference: this business books in "
        f"{zone_label(business_tz)} and your address is in {zone_label(buyer_tz)}, "
        "so please note that the times below are the business's local time, with "
        "your local time in parentheses."
    )


def slot_displays(
    start: Any, end: Any, business_tz: Optional[str], buyer_tz: Optional[str], *, dual: bool,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "business_start_display": format_instant(start, business_tz),
        "business_end_display": format_instant(end, business_tz),
        "business_timezone": valid_iana_timezone(business_tz),
    }
    if dual:
        out["buyer_start_display"] = format_instant(start, buyer_tz)
        out["buyer_end_display"] = format_instant(end, buyer_tz)
        out["buyer_timezone"] = valid_iana_timezone(buyer_tz)
    return out


def range_display(start: Any, end: Any, tz_name: Optional[str]) -> Optional[str]:
    begin = format_instant(start, tz_name)
    finish = format_instant(end, tz_name)
    if begin is None:
        return None
    return f"{begin} to {finish}" if finish else begin


def _clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt:%M}"


def compact_range_display(
    start: Any, end: Any, tz_name: Optional[str], *, now: Optional[datetime] = None,
) -> Optional[str]:
    """``Tue, Oct 13, 10:00 - 11:00 AM MST``; the year shows only when it is not the current one."""
    zone = valid_iana_timezone(tz_name)
    begin = _parse_instant(start)
    if zone is None or begin is None:
        return None
    tz = ZoneInfo(zone)
    local_start = begin.astimezone(tz)
    finish = _parse_instant(end)
    local_end = finish.astimezone(tz) if finish is not None else None
    this_year = (now or datetime.now(timezone.utc)).astimezone(tz).year

    def day(dt: datetime) -> str:
        text = f"{dt:%a, %b} {dt.day}"
        return text if dt.year == this_year else f"{text}, {dt.year}"

    if local_end is None:
        return f"{day(local_start)}, {_clock(local_start)} {local_start:%p} {local_start.tzname()}"
    if local_end.date() != local_start.date():
        return (
            f"{day(local_start)}, {_clock(local_start)} {local_start:%p} - "
            f"{day(local_end)}, {_clock(local_end)} {local_end:%p} {local_end.tzname()}"
        )
    if local_start.strftime("%p") == local_end.strftime("%p"):
        times = f"{_clock(local_start)} - {_clock(local_end)} {local_end:%p}"
    else:
        times = f"{_clock(local_start)} {local_start:%p} - {_clock(local_end)} {local_end:%p}"
    return f"{day(local_start)}, {times} {local_end.tzname()}"


def compact_clock_range(
    start: Any, end: Any, tz_name: Optional[str], *, reference_tz: Optional[str] = None,
) -> Optional[str]:
    """``8:00 - 9:00 PM IDT``: the time of day for a second zone shown in parentheses.

    The weekday is added when that day differs from the day in ``reference_tz``.
    """
    zone = valid_iana_timezone(tz_name)
    begin, finish = _parse_instant(start), _parse_instant(end)
    if zone is None or begin is None:
        return None
    tz = ZoneInfo(zone)
    a = begin.astimezone(tz)
    ref = valid_iana_timezone(reference_tz)
    ref_day = begin.astimezone(ZoneInfo(ref)).date() if ref else a.date()
    prefix = f"{a:%a} " if a.date() != ref_day else ""
    if finish is None:
        return f"{prefix}{_clock(a)} {a:%p} {a.tzname()}"
    b = finish.astimezone(tz)
    if b.date() != a.date():
        return f"{prefix}{_clock(a)} {a:%p} - {b:%a} {_clock(b)} {b:%p} {b.tzname()}"
    if a.strftime("%p") == b.strftime("%p"):
        return f"{prefix}{_clock(a)} - {_clock(b)} {b:%p} {b.tzname()}"
    return f"{prefix}{_clock(a)} {a:%p} - {_clock(b)} {b:%p} {b.tzname()}"


@lru_cache(maxsize=1)
def _finder():
    try:
        from timezonefinder import TimezoneFinder
    except ImportError:
        raise BuyerTimezoneError("timezone_lookup_dependency_missing") from None

    return TimezoneFinder()


atexit.register(_finder.cache_clear)


def timezone_for_coordinates(lat: float, lon: float) -> str:
    zone = valid_iana_timezone(_finder().timezone_at(lng=lon, lat=lat))
    if zone is None:
        raise BuyerTimezoneError("timezone_not_found")
    return zone


# A zone whose UTC offset is this far from the longitude's solar offset cannot be the
# local zone of that point (widest real gap is western China, about 3 hours).
_LONGITUDE_OFFSET_TOLERANCE_HOURS = 4.5


def service_location_timezone_conflict(
    business_tz: Optional[str],
    coordinates: Optional[Tuple[float, float]],
    instants: Iterable[Any],
) -> Optional[Dict[str, Any]]:
    """Describe a conflict when the business zone cannot be local time at the service location.

    Uses the exact zone at the coordinates when ``timezonefinder`` is installed, and a
    longitude-offset plausibility check otherwise, so a missing dependency never hides
    a merchant publishing, say, ``Asia/Jerusalem`` for a service in Yuma, Arizona.
    """
    biz = valid_iana_timezone(business_tz)
    if biz is None or coordinates is None:
        return None
    lat, lng = coordinates
    points = [dt for dt in (_parse_instant(v) for v in instants) if dt is not None]
    if not points:
        points = [datetime.now(timezone.utc)]
    try:
        location_zone = timezone_for_coordinates(lat, lng)
    except BuyerTimezoneError:
        location_zone = None
    if location_zone is not None:
        if not zones_differ(biz, location_zone, points):
            return None
        return {
            "business_timezone": biz,
            "service_location_timezone": location_zone,
            "service_coordinates": {"lat": lat, "lng": lng},
            "method": "timezonefinder",
        }
    solar_offset = lng / 15.0
    offsets = [dt.astimezone(ZoneInfo(biz)).utcoffset() for dt in points]
    gap = max(abs(o.total_seconds() / 3600.0 - solar_offset) for o in offsets if o is not None)
    if gap <= _LONGITUDE_OFFSET_TOLERANCE_HOURS:
        return None
    return {
        "business_timezone": biz,
        "service_location_timezone": None,
        "service_coordinates": {"lat": lat, "lng": lng},
        "offset_gap_hours": round(gap, 1),
        "method": "longitude_offset",
    }


_ISO_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})"
)


def localize_instants_in_text(text: Any, tz_name: Optional[str]) -> Any:
    """Replace RFC 3339 timestamps inside merchant text with ``format_instant`` displays."""
    if not isinstance(text, str) or valid_iana_timezone(tz_name) is None:
        return text
    return _ISO_INSTANT.sub(lambda m: format_instant(m.group(0), tz_name) or m.group(0), text)


def lookup_buyer_timezone(
    fetch_default_address: Callable[[], Optional[Dict[str, Any]]],
) -> BuyerTimezoneResult:
    """Resolve the buyer zone from the default Link shipping address only.

    ``fetch_default_address`` returns the address object or raises an exception
    carrying a sanitized ``code``. Every failure becomes an error code; the
    address itself is never returned, stored, or logged.
    """
    try:
        address = fetch_default_address()
        if not address:
            raise BuyerTimezoneError("no_default_address")
        lat, lon = geocode_address(address)
        zone = timezone_for_coordinates(lat, lon)
    except BuyerTimezoneError as e:
        return BuyerTimezoneResult(error=e.code)
    except Exception as e:
        code = getattr(e, "code", None)
        return BuyerTimezoneResult(error=code if isinstance(code, str) else "link_address_unavailable")
    return BuyerTimezoneResult(timezone=zone, source=BUYER_TIMEZONE_SOURCE)
