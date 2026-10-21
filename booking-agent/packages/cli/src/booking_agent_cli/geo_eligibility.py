"""Geography helpers for registry radius and location facts."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from booking_agent.models.wire import Link, Location, USPService


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in kilometers."""
    r = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def _coordinates_from_location(loc: Any) -> Optional[Tuple[float, float]]:
    if loc is None:
        return None
    if isinstance(loc, dict):
        coords = loc.get("coordinates")
        if isinstance(coords, dict):
            lat = coords.get("lat")
            lng = coords.get("lng")
            if lat is not None and lng is not None:
                return float(lat), float(lng)
        return None
    coords = getattr(loc, "coordinates", None)
    if coords is None:
        return None
    lat = getattr(coords, "lat", None)
    lng = getattr(coords, "lng", None)
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


def nearest_location_distance_km(
    search_lat: float,
    search_lng: float,
    locations: Optional[List[Any]],
) -> Optional[Tuple[float, str]]:
    """Return (distance_km, location_name) for the nearest location with coordinates."""
    if not locations:
        return None
    best: Optional[Tuple[float, str]] = None
    for loc in locations:
        coords = _coordinates_from_location(loc)
        if coords is None:
            continue
        lat, lng = coords
        dist = haversine_km(search_lat, search_lng, lat, lng)
        name = ""
        if isinstance(loc, dict):
            name = str(loc.get("name") or loc.get("id") or "")
        else:
            name = str(getattr(loc, "name", None) or getattr(loc, "id", None) or "")
        if best is None or dist < best[0]:
            best = (dist, name)
    return best


def serialize_locations(locations: Optional[List[Location]]) -> Optional[List[Dict[str, Any]]]:
    if not locations:
        return None
    out: List[Dict[str, Any]] = []
    for loc in locations:
        entry: Dict[str, Any] = {}
        if loc.id:
            entry["id"] = loc.id
        if loc.name:
            entry["name"] = loc.name
        if loc.address:
            entry["address"] = loc.address
        if loc.coordinates is not None:
            entry["coordinates"] = {
                "lat": loc.coordinates.lat,
                "lng": loc.coordinates.lng,
            }
        if entry:
            out.append(entry)
    return out or None


def serialize_service_links(links: Optional[List[Link]]) -> Optional[List[Dict[str, Any]]]:
    if not links:
        return None
    out = []
    for link in links:
        row: Dict[str, Any] = {"type": link.type, "url": link.url}
        if link.title:
            row["title"] = link.title
        out.append(row)
    return out


def location_block_from_service(service: USPService) -> Dict[str, Any]:
    """Catalog location fields for agent JSON (omit when absent)."""
    block: Dict[str, Any] = {}
    locs = serialize_locations(service.locations)
    if locs:
        block["locations"] = locs
    channel = service.channel
    if channel is not None:
        ch: Dict[str, Any] = {"type": str(getattr(channel.type, "value", channel.type))}
        if channel.service_area:
            ch["service_area"] = channel.service_area
        block["channel"] = ch
    links = serialize_service_links(service.links)
    if links:
        block["links"] = links
    return block


def distance_facts_for_session(
    session: Any,
    service_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Report nearest service location when discover coordinates exist."""
    lat = getattr(session, "discover_location_lat", None)
    lng = getattr(session, "discover_location_lng", None)
    if lat is None or lng is None:
        return None
    nearest = nearest_location_distance_km(
        float(lat),
        float(lng),
        service_data.get("locations"),
    )
    if nearest is None:
        return None
    dist_km, name = nearest
    facts: Dict[str, Any] = {"distance_km": round(dist_km, 1)}
    if name:
        facts["nearest_location_name"] = name
    radius = getattr(session, "discover_location_radius_km", None)
    if radius is not None:
        facts["search_radius_km"] = float(radius)
    return facts


def geo_refusal_for_session(
    session: Any,
    service_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Refuse only when a radius was sent and the service has coordinates outside it."""
    radius = getattr(session, "discover_location_radius_km", None)
    if radius is None:
        return None
    lat = getattr(session, "discover_location_lat", None)
    lng = getattr(session, "discover_location_lng", None)
    if lat is None or lng is None:
        return None
    nearest = nearest_location_distance_km(
        float(lat),
        float(lng),
        service_data.get("locations"),
    )
    if nearest is None:
        return None
    dist_km, name = nearest
    if dist_km <= float(radius):
        return None
    location_label = name or "the service location"
    return {
        "code": "service_not_available_in_area",
        "message": (
            f"This service is about {dist_km:.1f} km from your search area "
            f"({location_label}), outside the {float(radius):g} km limit. "
            "Registry search should not have returned it "
            "(see universal-scheduling-protocol-spec#225)."
        ),
        "distance_km": round(dist_km, 1),
        "search_radius_km": float(radius),
        "nearest_location_name": name or None,
        "registry_issue": 225,
    }
