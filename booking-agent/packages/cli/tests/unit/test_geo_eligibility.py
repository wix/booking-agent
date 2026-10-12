"""Track A geography helpers (issue #197)."""
from __future__ import annotations

from booking_agent_cli.geo_eligibility import (
    geo_refusal_for_session,
    haversine_km,
    nearest_location_distance_km,
)
from booking_agent_cli.session import Session


def test_haversine_km_same_point_is_zero():
    assert haversine_km(32.69, -114.62, 32.69, -114.62) == 0.0


def test_nearest_location_distance_km():
    dist, name = nearest_location_distance_km(
        32.6927,
        -114.6277,
        [{"name": "Yuma", "coordinates": {"lat": 32.69, "lng": -114.62}}],
    )
    assert dist is not None
    assert dist < 5
    assert name == "Yuma"


def test_geo_refusal_when_outside_sent_radius():
    session = Session(
        discover_location_lat=32.6927,
        discover_location_lng=-114.6277,
        discover_location_radius_km=25.0,
    )
    service = {
        "locations": [
            {"name": "Manhattan", "coordinates": {"lat": 40.7128, "lng": -74.0060}}
        ]
    }
    refusal = geo_refusal_for_session(session, service)
    assert refusal is not None
    assert refusal["code"] == "service_not_available_in_area"
    assert refusal["search_radius_km"] == 25.0


def test_geo_refusal_none_without_radius():
    session = Session(
        discover_location_lat=32.6927,
        discover_location_lng=-114.6277,
    )
    service = {
        "locations": [
            {"name": "Manhattan", "coordinates": {"lat": 40.7128, "lng": -74.0060}}
        ]
    }
    assert geo_refusal_for_session(session, service) is None


def test_geo_refusal_none_for_online_service_without_coordinates():
    session = Session(
        discover_location_lat=32.6927,
        discover_location_lng=-114.6277,
        discover_location_radius_km=25.0,
    )
    assert geo_refusal_for_session(session, {}) is None
