"""Geocoder module. The HTTP call is always faked."""
from __future__ import annotations

import pytest

import booking_agent_cli.geocode as geo

_ROME_ST = {
    "lat": "32.6634",
    "lon": "-114.6602",
    "display_name": "3194, West Rome Street, Yuma, Yuma County, Arizona, 85364, United States",
    "address": {
        "house_number": "3194",
        "road": "West Rome Street",
        "city": "Yuma",
        "state": "Arizona",
        "ISO3166-2-lvl4": "US-AZ",
        "postcode": "85364",
        "country_code": "us",
    },
}
_YUMA_CITY = {
    "lat": "32.6927",
    "lon": "-114.6277",
    "display_name": "Yuma, Yuma County, Arizona, United States",
    "address": {"city": "Yuma", "state": "Arizona", "country_code": "us"},
}


def _fake(monkeypatch, rows, calls):
    def fake(params):
        calls.append(dict(params))
        return rows

    monkeypatch.setattr(geo, "_geocode_http", fake)


def test_geocode_text_street_level_returns_structured_address(monkeypatch):
    calls = []
    _fake(monkeypatch, [_ROME_ST], calls)
    result = geo.geocode_text("3194 W Rome St, Yuma AZ", country_code="US")
    assert result.street_level is True
    assert result.address == {
        "line1": "3194 West Rome Street",
        "city": "Yuma",
        "region": "AZ",
        "postal_code": "85364",
        "country": "US",
    }
    assert geo.address_label(result.address) == "3194 West Rome Street, Yuma AZ 85364"
    assert calls == [{"q": "3194 W Rome St, Yuma AZ", "countrycodes": "us"}]


def test_geocode_text_city_match_is_not_street_level(monkeypatch):
    _fake(monkeypatch, [_YUMA_CITY], [])
    result = geo.geocode_text("Yuma, AZ")
    assert result.street_level is False
    assert result.address == {}
    assert result.display_name.startswith("Yuma")
    assert (result.lat, result.lng) == (32.6927, -114.6277)


def test_geocode_text_no_match_and_empty(monkeypatch):
    _fake(monkeypatch, [], [])
    with pytest.raises(geo.GeocodeError) as err:
        geo.geocode_text("nowhere at all")
    assert err.value.code == "geocoder_no_match"
    with pytest.raises(geo.GeocodeError) as err:
        geo.geocode_text("  ")
    assert err.value.code == "address_incomplete"


def test_repeat_lookups_are_cached(monkeypatch):
    calls = []
    _fake(monkeypatch, [_YUMA_CITY], calls)
    geo.geocode_text("Yuma, AZ")
    geo.geocode_text("Yuma, AZ")
    assert len(calls) == 1


def test_structured_link_address_is_street_level_when_street_query_hits(monkeypatch):
    _fake(monkeypatch, [_ROME_ST], [])
    result = geo.geocode_structured({
        "line_1": "3194 W Rome St", "locality": "Yuma", "administrative_area": "AZ",
        "postal_code": "85364", "country_code": "US",
    })
    assert result.street_level is True
    assert result.address["line1"] == "3194 W Rome St"
    assert result.address["country"] == "US"


def test_requests_are_spaced_one_second_apart(monkeypatch):
    clock = {"now": 100.0}
    sleeps = []
    monkeypatch.setattr(geo.time, "monotonic", lambda: clock["now"])

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(geo.time, "sleep", sleep)
    geo._respect_rate_limit()
    clock["now"] += 0.25
    geo._respect_rate_limit()
    assert sleeps == [pytest.approx(0.75)]
