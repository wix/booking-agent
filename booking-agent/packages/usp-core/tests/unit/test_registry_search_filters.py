"""Registry search filter wire shape (USP §6.3.1 / issue #59)."""

from booking_agent.models.platform import (
    DesiredServiceTimeFrom,
    DesiredServiceTimeMoment,
    DesiredServiceTimeRange,
    DurationRange,
    PriceRange,
    RangeMatchMode,
    ServiceSearchLocation,
)
from booking_agent.models.wire import Coordinates
from booking_agent.rest_client import build_registry_search_body


def test_range_match_mode_values_match_spec_enum():
    assert [m.value for m in RangeMatchMode] == [
        "overlap",
        "contained",
        "contains",
        "equals",
    ]


def test_build_registry_search_body_omits_match_when_unset():
    body = build_registry_search_body(
        "ac repair",
        price_range=PriceRange(min=5000, max=20000, currency="USD"),
        duration_range=DurationRange(min_minutes=30, max_minutes=90),
    )
    assert body["price_range"] == {"min": 5000, "max": 20000, "currency": "USD"}
    assert body["duration_range"] == {"minMinutes": 30, "maxMinutes": 90}
    assert "match" not in body["price_range"]
    assert "match" not in body["duration_range"]


def test_build_registry_search_body_serializes_match_modes():
    body = build_registry_search_body(
        "haircut",
        location=ServiceSearchLocation(
            coordinates=Coordinates(lat=40.71, lng=-74.0),
            radius_km=5,
        ),
        price_range=PriceRange(
            min=8000,
            max=10000,
            currency="USD",
            match=RangeMatchMode.CONTAINS,
        ),
        duration_range=DurationRange(
            min_minutes=60,
            max_minutes=60,
            match=RangeMatchMode.OVERLAP,
        ),
    )
    assert body["price_range"]["match"] == "contains"
    assert body["duration_range"]["match"] == "overlap"
    assert body["location"]["radiusKm"] == 5


def test_build_registry_search_body_serializes_time_preference_shapes():
    body = build_registry_search_body(
        "massage",
        desired_service_time_ranges=[
            DesiredServiceTimeMoment(at="2026-09-01T12:00:00Z"),
            DesiredServiceTimeRange(
                start="2026-09-02T09:00:00Z",
                end="2026-09-02T17:00:00Z",
            ),
            DesiredServiceTimeFrom(start="2026-09-03T08:00:00Z"),
        ],
    )
    assert body["desired_service_time_ranges"] == [
        {"at": "2026-09-01T12:00:00Z"},
        {
            "start": "2026-09-02T09:00:00Z",
            "end": "2026-09-02T17:00:00Z",
        },
        {"start": "2026-09-03T08:00:00Z"},
    ]


def test_build_registry_search_body_only_sends_explicit_false_for_soonness():
    default_body = build_registry_search_body(
        "massage", prefer_sooner_availability_slots=None
    )
    true_body = build_registry_search_body(
        "massage", prefer_sooner_availability_slots=True
    )
    false_body = build_registry_search_body(
        "massage", prefer_sooner_availability_slots=False
    )
    assert "prefer_sooner_availability_slots" not in default_body
    assert "prefer_sooner_availability_slots" not in true_body
    assert false_body["prefer_sooner_availability_slots"] is False


def test_build_registry_search_body_omits_ranking_fields_without_filter():
    body = build_registry_search_body(
        "",
        desired_service_time_ranges=[
            DesiredServiceTimeFrom(start="2026-09-01T12:00:00Z")
        ],
        prefer_sooner_availability_slots=False,
        limit=5,
    )
    assert body == {"pagination": {"limit": 5}}


def test_search_intent_sends_no_vertical_filter_by_default():
    from booking_agent.headless import build_search_intent

    intent = build_search_intent(query="yoga class")
    assert intent.verticals == []
    assert "verticals" not in build_registry_search_body(intent.query, verticals=intent.verticals)


def test_parse_service_verticals_accepts_core_and_custom_values():
    from booking_agent.headless import parse_service_verticals

    assert parse_service_verticals(
        ["group", " field_service ", "group", "com.wix.services.courses", ""]
    ) == ["group", "field_service", "com.wix.services.courses"]
    assert parse_service_verticals(None) == []


def test_parse_service_verticals_rejects_unknown_values():
    import pytest

    from booking_agent.headless import parse_service_verticals

    for bad in ("class", "Appointment", "com.wix"):
        with pytest.raises(ValueError):
            parse_service_verticals([bad])
