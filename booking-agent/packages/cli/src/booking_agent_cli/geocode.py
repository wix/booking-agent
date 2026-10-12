"""Address geocoding over HTTPS (OpenStreetMap Nominatim by default).

Used for the buyer timezone and for the search area / service address the
user confirms before a place-based search or a home visit. Errors carry a
sanitized ``code`` that never contains the address or the geocoder URL.

The default endpoint follows the Nominatim usage policy: an identifying
User-Agent, at most one request per second, and cached repeat lookups.
``BOOKING_AGENT_GEOCODER_URL`` points at any HTTPS service with the same
``/search`` contract (for example a self-hosted Nominatim).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from booking_agent import settings

GEOCODER_URL_ENV = "BOOKING_AGENT_GEOCODER_URL"
GEOCODER_USER_AGENT = "booking-agent-cli/0.1 (+https://github.com/wix/booking-agent)"
GEOCODER_TIMEOUT_SECONDS = 10.0
MIN_REQUEST_INTERVAL_SECONDS = 1.0


class GeocodeError(Exception):
    """Sanitized geocoding failure. ``code`` never contains the address or URL."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class GeocodeResult:
    lat: float
    lng: float
    display_name: str = ""
    street_level: bool = False
    # Structured USP DeliveryAddress fields when the match is precise enough.
    address: Dict[str, Any] = field(default_factory=dict)


_cache: Dict[Tuple[Tuple[str, str], ...], List[Dict[str, Any]]] = {}
_rate_lock = threading.Lock()
_last_request_monotonic = 0.0


def clear_cache() -> None:
    global _last_request_monotonic
    _cache.clear()
    _last_request_monotonic = 0.0


def geocoder_url() -> str:
    url = (settings.get(GEOCODER_URL_ENV) or "").strip() or settings.shipped_default(GEOCODER_URL_ENV)
    if urlparse(url).scheme != "https":
        raise GeocodeError("geocoder_url_invalid")
    return url


def _respect_rate_limit() -> None:
    global _last_request_monotonic
    with _rate_lock:
        wait = _last_request_monotonic + MIN_REQUEST_INTERVAL_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_monotonic = time.monotonic()


def _geocode_http(params: Dict[str, str]) -> List[Dict[str, Any]]:
    url = geocoder_url()
    _respect_rate_limit()
    try:
        response = httpx.get(
            url,
            params={**params, "format": "jsonv2", "limit": "1", "addressdetails": "1"},
            headers={"User-Agent": GEOCODER_USER_AGENT, "Accept": "application/json"},
            timeout=GEOCODER_TIMEOUT_SECONDS,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError:
        raise GeocodeError("geocoder_http_error") from None
    except (httpx.HTTPError, ValueError):
        raise GeocodeError("geocoder_unavailable") from None
    return payload if isinstance(payload, list) else []


def _lookup(params: Dict[str, str]) -> List[Dict[str, Any]]:
    key = tuple(sorted(params.items()))
    if key not in _cache:
        _cache[key] = _geocode_http(params)
    return _cache[key]


def _pick(address: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _geocode_queries(address: Dict[str, Any]) -> List[Dict[str, str]]:
    street = _pick(address, "line_1", "line1", "address_line_1")
    city = _pick(address, "locality", "city")
    state = _pick(address, "administrative_area", "state", "region")
    postal = _pick(address, "postal_code", "zip")
    country = _pick(address, "country_code", "country")
    if not country or not (city or postal):
        raise GeocodeError("address_incomplete")
    coarse = {k: v for k, v in (
        ("city", city), ("state", state), ("postalcode", postal),
    ) if v}
    queries = []
    if street:
        queries.append({"street": street, **coarse})
    queries.append(coarse)
    return [{**q, "countrycodes": country.lower()} for q in queries]


def _coords(row: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    try:
        return float(row["lat"]), float(row["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def geocode_address(address: Dict[str, Any]) -> Tuple[float, float]:
    """``(lat, lon)`` for a Link shipping address, trying a street-level then coarse query."""
    result = geocode_structured(address)
    return result.lat, result.lng


def delivery_address_from_link(address: Dict[str, Any]) -> Dict[str, Any]:
    """Map a Link shipping address to USP ``DeliveryAddress`` fields."""
    out = {
        "line1": _pick(address, "line_1", "line1", "address_line_1"),
        "line2": _pick(address, "line_2", "line2", "address_line_2"),
        "city": _pick(address, "locality", "city"),
        "region": _pick(address, "administrative_area", "state", "region"),
        "postal_code": _pick(address, "postal_code", "zip"),
        "country": (_pick(address, "country_code", "country") or "").upper() or None,
    }
    return {k: v for k, v in out.items() if v}


def geocode_structured(address: Dict[str, Any]) -> GeocodeResult:
    """Geocode a structured (Link-style) address. The match is street-level when
    the street query hit; the structured address is the Link address itself."""
    queries = _geocode_queries(address)
    for index, params in enumerate(queries):
        for row in _lookup(params):
            coords = _coords(row)
            if coords is None:
                continue
            street_level = "street" in params and index == 0
            structured = delivery_address_from_link(address)
            return GeocodeResult(
                lat=coords[0],
                lng=coords[1],
                display_name=str(row.get("display_name") or ""),
                street_level=street_level and bool(structured.get("line1")),
                address=structured,
            )
    raise GeocodeError("geocoder_no_match")


def _region_code(details: Dict[str, Any]) -> Optional[str]:
    iso = details.get("ISO3166-2-lvl4") or details.get("ISO3166-2-lvl6")
    if isinstance(iso, str) and "-" in iso:
        return iso.split("-", 1)[1]
    state = details.get("state")
    return state if isinstance(state, str) and state else None


def _address_from_details(details: Dict[str, Any]) -> Dict[str, Any]:
    road = details.get("road") or details.get("pedestrian") or details.get("street")
    number = details.get("house_number")
    line1 = " ".join(str(p) for p in (number, road) if p) or None
    city = next(
        (details.get(k) for k in ("city", "town", "village", "hamlet", "municipality")
         if details.get(k)),
        None,
    )
    out = {
        "line1": line1,
        "city": city,
        "region": _region_code(details),
        "postal_code": details.get("postcode"),
        "country": str(details.get("country_code") or "").upper() or None,
    }
    return {k: v for k, v in out.items() if v}


def geocode_text(text: str, *, country_code: Optional[str] = None) -> GeocodeResult:
    """Geocode an address or area the user typed. Street-level only when the
    match has both a house number and a road."""
    query = (text or "").strip()
    if not query:
        raise GeocodeError("address_incomplete")
    params: Dict[str, str] = {"q": query}
    if country_code:
        params["countrycodes"] = country_code.lower()
    for row in _lookup(params):
        coords = _coords(row)
        if coords is None:
            continue
        details = row.get("address") if isinstance(row.get("address"), dict) else {}
        structured = _address_from_details(details)
        street_level = bool(details.get("house_number") and structured.get("line1"))
        return GeocodeResult(
            lat=coords[0],
            lng=coords[1],
            display_name=str(row.get("display_name") or ""),
            street_level=street_level and bool(structured.get("city") and structured.get("country")),
            address=structured if street_level else {},
        )
    raise GeocodeError("geocoder_no_match")


def address_label(address: Dict[str, Any]) -> str:
    """``3194 West Rome Street, Yuma AZ 85364`` from DeliveryAddress fields."""
    line = ", ".join(p for p in (address.get("line1"), address.get("line2")) if p)
    locality = " ".join(p for p in (address.get("city"), address.get("region")) if p)
    tail = " ".join(p for p in (locality, address.get("postal_code")) if p)
    return ", ".join(p for p in (line, tail) if p)
