"""flow search-area propose/set/confirm. Geocoder and link-cli are faked."""
from __future__ import annotations

import json

import pytest

import booking_agent_cli.geocode as geo
from booking_agent_cli.main import app
from booking_agent_cli.session import load_session
from support.mock.link_cli import YUMA_SHIPPING_ADDRESS, mock_link_cli_user_info

_BUYER = {"name": "Ada Lovelace", "email": "ada@booking-agent.test"}
_OFFICE = {
    "id": "csmrsa_test_office",
    "is_default": False,
    "address": {
        "line_1": "1 Main Street", "locality": "Phoenix", "administrative_area": "AZ",
        "postal_code": "85001", "country_code": "US",
    },
}
_STREET_ROW = {
    "lat": "32.6927", "lon": "-114.6277",
    "display_name": "100, Test Street, Yuma, Arizona, 85364, United States",
}
_TYPED_STREET_ROW = {
    "lat": "32.6634", "lon": "-114.6602",
    "display_name": "3194, West Rome Street, Yuma, Arizona, 85364, United States",
    "address": {
        "house_number": "3194", "road": "West Rome Street", "city": "Yuma",
        "ISO3166-2-lvl4": "US-AZ", "postcode": "85364", "country_code": "us",
    },
}
_CITY_ROW = {
    "lat": "33.4484", "lon": "-112.0740",
    "display_name": "Phoenix, Maricopa County, Arizona, United States",
    "address": {"city": "Phoenix", "state": "Arizona", "country_code": "us"},
}


def _out(result):
    return json.loads(result.stdout.strip().splitlines()[-1]) if "\n" not in result.stdout.strip() \
        else json.loads(result.stdout)


@pytest.fixture
def area_session(isolated_session, runner, monkeypatch):
    def fake(params):
        if params.get("q", "").lower().startswith("phoenix"):
            return [_CITY_ROW]
        if "q" in params:
            return [_TYPED_STREET_ROW]
        return [_STREET_ROW]

    monkeypatch.setattr(geo, "_geocode_http", fake)
    mock_link_cli_user_info(
        monkeypatch, _BUYER, shipping_addresses=[YUMA_SHIPPING_ADDRESS, _OFFICE],
    )
    r = runner.invoke(app, ["flow", "init", "--user-request", "AC repair"])
    assert r.exit_code == 0, r.stdout
    return runner


def test_propose_uses_default_link_address_and_lists_others(area_session):
    r = area_session.invoke(app, ["flow", "search-area", "propose"])
    assert r.exit_code == 0, r.stdout
    body = _out(r)
    assert body["awaiting_user"] == "search_area_confirmation"
    assert body["data"]["proposed_area"]["label"] == "100 Test Street, Yuma AZ 85364"
    assert body["data"]["proposed_area"]["street_level"] is True
    assert body["data"]["other_addresses"] == [
        {"id": "csmrsa_test_office", "label": "1 Main Street, Phoenix AZ 85001", "is_default": False}
    ]
    assert body["next_command"] == "booking-agent flow search-area confirm --user-confirmed"
    assert "lat" not in body["data"]["proposed_area"]


def test_confirm_requires_user_confirmed(area_session):
    area_session.invoke(app, ["flow", "search-area", "propose"])
    r = area_session.invoke(app, ["flow", "search-area", "confirm"])
    assert r.exit_code == 1
    assert _out(r)["data"]["error"]["code"] == "search_area_not_user_confirmed"
    assert load_session().search_area is None


def test_confirm_saves_area_and_service_address(area_session):
    area_session.invoke(app, ["flow", "search-area", "propose"])
    r = area_session.invoke(app, ["flow", "search-area", "confirm", "--user-confirmed"])
    assert r.exit_code == 0, r.stdout
    assert _out(r)["next_command"] == "booking-agent flow discover --radius-km <km>"
    session = load_session()
    assert session.search_area["confirmed"] is True
    assert session.search_area["lat"] == pytest.approx(32.6927)
    assert session.service_address["line1"] == "100 Test Street"
    assert session.service_address["coordinates"] == {"lat": 32.6927, "lng": -114.6277}
    assert session.service_address_label == "100 Test Street, Yuma AZ 85364"
    assert session.pending_area is None


def test_typed_address_shows_match_and_needs_confirmation(area_session):
    r = area_session.invoke(app, ["flow", "search-area", "set", "--address", "3194 w rome st yuma"])
    body = _out(r)
    assert body["awaiting_user"] == "search_area_confirmation"
    assert body["data"]["proposed_area"]["typed"] == "3194 w rome st yuma"
    assert body["data"]["proposed_area"]["matched"].startswith("3194, West Rome Street")
    assert "matched by the map lookup" in body["prompt"]
    assert load_session().search_area is None
    area_session.invoke(app, ["flow", "search-area", "confirm", "--user-confirmed"])
    assert load_session().service_address_label == "3194 West Rome Street, Yuma AZ 85364"


def test_typed_city_sets_area_without_service_address(area_session):
    area_session.invoke(app, ["flow", "search-area", "set", "--address", "Phoenix, AZ"])
    area_session.invoke(app, ["flow", "search-area", "confirm", "--user-confirmed"])
    session = load_session()
    assert session.search_area["street_level"] is False
    assert session.service_address is None


def test_pick_other_link_address(area_session):
    r = area_session.invoke(
        app, ["flow", "search-area", "set", "--link-address-id", "csmrsa_test_office"],
    )
    assert _out(r)["data"]["proposed_area"]["label"] == "1 Main Street, Phoenix AZ 85001"


def test_service_address_purpose_refuses_an_area(area_session):
    area_session.invoke(
        app, ["flow", "search-area", "set", "--address", "Phoenix", "--purpose", "service-address"],
    )
    r = area_session.invoke(app, ["flow", "search-area", "confirm", "--user-confirmed"])
    body = _out(r)
    assert body["awaiting_user"] == "service_address_input"
    assert "--purpose service-address" in body["next_command"]
    assert load_session().service_address is None


def test_no_link_address_asks_the_user(isolated_session, runner, monkeypatch):
    mock_link_cli_user_info(monkeypatch, _BUYER, shipping_addresses=[])
    runner.invoke(app, ["flow", "init", "--user-request", "AC repair"])
    body = _out(runner.invoke(app, ["flow", "search-area", "propose"]))
    assert body["awaiting_user"] == "search_area_input"
    assert "search-area set --address" in body["next_command"]


def test_geocoder_failure_never_proposes_an_unlocated_area(area_session, monkeypatch):
    def down(_params):
        raise geo.GeocodeError("geocoder_unavailable")

    monkeypatch.setattr(geo, "_geocode_http", down)
    body = _out(area_session.invoke(app, ["flow", "search-area", "propose"]))
    assert body["awaiting_user"] == "search_area_input"
    assert body["data"]["geocode_error"] == "geocoder_unavailable"
    assert "label" not in (load_session().pending_area or {})
