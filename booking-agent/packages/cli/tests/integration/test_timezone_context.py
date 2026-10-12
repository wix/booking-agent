"""Business/buyer timezone helpers. The geocoder is always mocked."""
from __future__ import annotations

import httpx
import pytest

import booking_agent_cli.geocode as geo
import booking_agent_cli.timezone_context as tc
from booking_agent_cli.link_cli import (
    LinkCliShippingAddressError,
    default_shipping_address,
    retrieve_default_shipping_address,
)
from support.mock.link_cli import YUMA_SHIPPING_ADDRESS, mock_link_cli_user_info

YUMA = YUMA_SHIPPING_ADDRESS["address"]
_REAL_GEOCODE_HTTP = geo._geocode_http


@pytest.fixture
def default_geocoder(monkeypatch):
    monkeypatch.delenv(geo.GEOCODER_URL_ENV, raising=False)


def _geocoder_returning(monkeypatch, rows, calls=None):
    def fake(params):
        if calls is not None:
            calls.append(dict(params))
        return rows(params) if callable(rows) else rows

    monkeypatch.setattr(geo, "_geocode_http", fake)


def test_valid_iana_timezone():
    assert tc.valid_iana_timezone("Asia/Jerusalem") == "Asia/Jerusalem"
    assert tc.valid_iana_timezone(" America/Phoenix ") == "America/Phoenix"
    assert tc.valid_iana_timezone("Mars/Olympus") is None
    assert tc.valid_iana_timezone("") is None
    assert tc.valid_iana_timezone(None) is None


def test_format_instant_names_zone_and_abbreviation():
    instant = "2026-10-06T14:00:00+00:00"
    assert tc.format_instant(instant, "America/Phoenix") == (
        "Tue, Oct 6, 2026, 7:00 AM MST (America/Phoenix)"
    )
    assert tc.format_instant(instant, "Asia/Jerusalem") == (
        "Tue, Oct 6, 2026, 5:00 PM IDT (Asia/Jerusalem)"
    )
    assert tc.format_instant("not-a-time", "Asia/Jerusalem") is None
    assert tc.format_instant(instant, "Not/AZone") is None


def test_format_instant_without_zone_keeps_the_slot_offset():
    assert tc.format_instant("2026-10-06T14:00:00-07:00", None) == (
        "Tue, Oct 6, 2026, 2:00 PM (UTC-07:00)"
    )
    assert tc.format_instant("2026-10-06T14:00:00Z", None) == (
        "Tue, Oct 6, 2026, 2:00 PM (UTC+00:00)"
    )
    assert tc.format_instant("2026-10-06T14:00:00", None) is None
    assert tc.format_instant("not-a-time", None) is None


def test_format_instant_across_jerusalem_dst_end():
    # Israel leaves daylight time on Sunday Oct 25 2026; Phoenix never observes it.
    before = "2026-10-24T14:00:00+00:00"
    after = "2026-10-26T14:00:00+00:00"
    assert tc.format_instant(before, "Asia/Jerusalem").startswith("Sat, Oct 24, 2026, 5:00 PM IDT")
    assert tc.format_instant(after, "Asia/Jerusalem").startswith("Mon, Oct 26, 2026, 4:00 PM IST")
    assert tc.format_instant(after, "America/Phoenix").startswith("Mon, Oct 26, 2026, 7:00 AM MST")


def test_zones_differ_uses_offsets_at_the_instants():
    assert tc.zones_differ("Asia/Jerusalem", "America/Phoenix", ["2026-10-06T14:00:00Z"])
    assert not tc.zones_differ("America/Phoenix", "America/Phoenix", [])
    # Phoenix matches Los Angeles in summer, not in winter.
    assert not tc.zones_differ("America/Phoenix", "America/Los_Angeles", ["2026-07-01T12:00:00Z"])
    assert tc.zones_differ("America/Phoenix", "America/Los_Angeles", ["2026-12-01T12:00:00Z"])
    assert not tc.zones_differ("Asia/Jerusalem", None, [])
    assert not tc.zones_differ("Asia/Jerusalem", "Mars/Olympus", [])


def test_timezone_notice_wording():
    notice = tc.timezone_notice("Asia/Jerusalem", "America/Phoenix")
    assert notice.startswith("I'm detecting a timezone difference")
    assert "Jerusalem time (Asia/Jerusalem)" in notice
    assert "Phoenix time (America/Phoenix)" in notice
    assert "so please note that" in notice
    assert "\u2014" not in notice and "\u2013" not in notice


def test_slot_displays_single_and_dual():
    single = tc.slot_displays(
        "2026-10-06T14:00:00Z", "2026-10-06T15:00:00Z", "Asia/Jerusalem", "America/Phoenix",
        dual=False,
    )
    assert single["business_timezone"] == "Asia/Jerusalem"
    assert single["business_start_display"].endswith("(Asia/Jerusalem)")
    assert "buyer_start_display" not in single
    dual = tc.slot_displays(
        "2026-10-06T14:00:00Z", "2026-10-06T15:00:00Z", "Asia/Jerusalem", "America/Phoenix",
        dual=True,
    )
    assert dual["buyer_timezone"] == "America/Phoenix"
    assert dual["buyer_start_display"] == "Tue, Oct 6, 2026, 7:00 AM MST (America/Phoenix)"
    assert dual["buyer_end_display"] == "Tue, Oct 6, 2026, 8:00 AM MST (America/Phoenix)"


def test_lookup_resolves_link_address_through_geocoder_and_timezonefinder(monkeypatch, default_geocoder):
    calls = []
    _geocoder_returning(monkeypatch, [{"lat": "32.6927", "lon": "-114.6277"}], calls)
    result = tc.lookup_buyer_timezone(lambda: dict(YUMA))
    assert result.timezone == "America/Phoenix"
    assert result.source == tc.BUYER_TIMEZONE_SOURCE
    assert result.error is None
    assert calls[0]["street"] == "100 Test Street"
    assert calls[0]["countrycodes"] == "us"


def test_lookup_falls_back_to_coarse_query_when_street_misses(monkeypatch, default_geocoder):
    calls = []
    _geocoder_returning(
        monkeypatch,
        lambda params: [] if "street" in params else [{"lat": "31.7683", "lon": "35.2137"}],
        calls,
    )
    result = tc.lookup_buyer_timezone(
        lambda: {"locality": "Jerusalem", "country_code": "IL", "line_1": "1 Unknown Rd"}
    )
    assert result.timezone == "Asia/Jerusalem"
    assert len(calls) == 2 and "street" not in calls[1]


@pytest.mark.parametrize(
    ("fetch", "rows", "code"),
    [
        (lambda: None, [], "no_default_address"),
        (lambda: {"locality": "Yuma"}, [], "address_incomplete"),
        (lambda: dict(YUMA), [], "geocoder_no_match"),
    ],
)
def test_lookup_failures_return_sanitized_codes(monkeypatch, default_geocoder, fetch, rows, code):
    _geocoder_returning(monkeypatch, rows)
    result = tc.lookup_buyer_timezone(fetch)
    assert result.timezone is None
    assert result.error == code


def test_lookup_maps_link_cli_errors_to_their_code(default_geocoder):
    def failing():
        raise LinkCliShippingAddressError("link_cli_failed")

    assert tc.lookup_buyer_timezone(failing).error == "link_cli_failed"


def test_lookup_never_uses_device_timezone(monkeypatch, default_geocoder):
    """A failed lookup yields no zone, whatever the host clock says."""
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    _geocoder_returning(monkeypatch, [])
    result = tc.lookup_buyer_timezone(lambda: dict(YUMA))
    assert result.timezone is None
    assert result.error == "geocoder_no_match"


def test_geocoder_http_errors_are_sanitized(monkeypatch, default_geocoder):
    monkeypatch.setattr(geo, "_geocode_http", _REAL_GEOCODE_HTTP)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(503, request=request)

    monkeypatch.setattr(
        geo.httpx, "get",
        lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).get(url, **kw),
    )
    with pytest.raises(tc.BuyerTimezoneError) as err:
        geo._geocode_http({"city": "Yuma", "countrycodes": "us"})
    assert err.value.code == "geocoder_http_error"
    assert "Yuma" not in str(err.value) and "nominatim" not in str(err.value)
    assert seen["ua"] == geo.GEOCODER_USER_AGENT


def test_geocoder_transport_failure_is_sanitized(monkeypatch, default_geocoder):
    monkeypatch.setattr(geo, "_geocode_http", _REAL_GEOCODE_HTTP)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(
        geo.httpx, "get",
        lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).get(url, **kw),
    )
    with pytest.raises(tc.BuyerTimezoneError) as err:
        geo._geocode_http({"city": "Yuma", "countrycodes": "us"})
    assert err.value.code == "geocoder_unavailable"


def test_geocoder_url_must_be_https(monkeypatch, default_geocoder):
    monkeypatch.setattr(geo, "_geocode_http", _REAL_GEOCODE_HTTP)
    monkeypatch.setenv(geo.GEOCODER_URL_ENV, "http://geo.example/search")
    with pytest.raises(tc.BuyerTimezoneError) as err:
        geo._geocode_http({"city": "Yuma", "countrycodes": "us"})
    assert err.value.code == "geocoder_url_invalid"


def test_missing_timezonefinder_has_its_own_error_code(monkeypatch, default_geocoder):
    """A broken install must not look like a missing Link address."""
    def no_finder():
        raise tc.BuyerTimezoneError("timezone_lookup_dependency_missing")

    monkeypatch.setattr(tc, "_finder", no_finder)
    _geocoder_returning(monkeypatch, [{"lat": "32.6927", "lon": "-114.6277"}])
    result = tc.lookup_buyer_timezone(lambda: dict(YUMA))
    assert result.timezone is None
    assert result.error == "timezone_lookup_dependency_missing"


_YUMA_COORDS = (32.6927, -114.6277)
_TUESDAY = ["2026-10-06T13:00:00Z"]


def test_location_conflict_detected_with_timezonefinder():
    conflict = tc.service_location_timezone_conflict("Asia/Jerusalem", _YUMA_COORDS, _TUESDAY)
    assert conflict["method"] == "timezonefinder"
    assert conflict["service_location_timezone"] == "America/Phoenix"
    assert conflict["business_timezone"] == "Asia/Jerusalem"


def test_location_conflict_falls_back_to_longitude_without_timezonefinder(monkeypatch):
    def no_finder():
        raise tc.BuyerTimezoneError("timezone_lookup_dependency_missing")

    monkeypatch.setattr(tc, "_finder", no_finder)
    conflict = tc.service_location_timezone_conflict("Asia/Jerusalem", _YUMA_COORDS, _TUESDAY)
    assert conflict["method"] == "longitude_offset"
    assert conflict["offset_gap_hours"] > 4.5
    assert tc.service_location_timezone_conflict(
        "America/Phoenix", _YUMA_COORDS, _TUESDAY,
    ) is None


def test_no_location_conflict_when_zone_matches_or_location_unknown():
    assert tc.service_location_timezone_conflict(
        "America/Phoenix", _YUMA_COORDS, _TUESDAY,
    ) is None
    assert tc.service_location_timezone_conflict("Asia/Jerusalem", None, _TUESDAY) is None
    assert tc.service_location_timezone_conflict(None, _YUMA_COORDS, _TUESDAY) is None


def test_localize_instants_in_merchant_text():
    text = "Deposit is non-refundable after 2026-10-05T13:00:00Z."
    assert tc.localize_instants_in_text(text, "Asia/Jerusalem") == (
        "Deposit is non-refundable after Mon, Oct 5, 2026, 4:00 PM IDT (Asia/Jerusalem)."
    )
    assert tc.localize_instants_in_text(text, None) == text
    assert tc.localize_instants_in_text(None, "Asia/Jerusalem") is None


def test_default_shipping_address_picks_default_entry():
    other = {"is_default": False, "address": {"locality": "Elsewhere"}}
    assert default_shipping_address([other, YUMA_SHIPPING_ADDRESS]) == YUMA
    assert default_shipping_address([{"address": {"locality": "Only"}}]) == {"locality": "Only"}
    assert default_shipping_address([other, {"is_default": False, "address": {}}]) is None
    assert default_shipping_address([]) is None


def test_retrieve_default_shipping_address_uses_link_cli(monkeypatch):
    mock_link_cli_user_info(
        monkeypatch, {"email": "ada@booking-agent.test"}, shipping_addresses=[YUMA_SHIPPING_ADDRESS],
    )
    assert retrieve_default_shipping_address() == YUMA


def test_compact_range_display():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    show = lambda a, b, z: tc.compact_range_display(a, b, z, now=now)  # noqa: E731
    assert show("2026-10-13T17:00:00Z", "2026-10-13T18:00:00Z", "America/Phoenix") == (
        "Tue, Oct 13, 10:00 - 11:00 AM MST"
    )
    assert show("2026-10-13T18:30:00Z", "2026-10-13T19:30:00Z", "America/Phoenix") == (
        "Tue, Oct 13, 11:30 AM - 12:30 PM MST"
    )
    assert show("2027-01-05T17:00:00Z", "2027-01-05T18:00:00Z", "America/Phoenix") == (
        "Tue, Jan 5, 2027, 10:00 - 11:00 AM MST"
    )
    assert show("2026-10-14T06:30:00Z", "2026-10-14T07:30:00Z", "America/Phoenix") == (
        "Tue, Oct 13, 11:30 PM - Wed, Oct 14, 12:30 AM MST"
    )
    assert show("2026-10-13T17:00:00Z", "2026-10-13T18:00:00Z", None) is None


def test_compact_clock_range_adds_the_weekday_when_the_day_differs():
    start, end = "2026-10-13T17:00:00Z", "2026-10-13T18:00:00Z"
    assert tc.compact_clock_range(start, end, "Asia/Jerusalem", reference_tz="America/Phoenix") == (
        "8:00 - 9:00 PM IDT"
    )
    assert tc.compact_clock_range(
        "2026-10-14T03:00:00Z", "2026-10-14T04:00:00Z", "Asia/Jerusalem",
        reference_tz="America/Phoenix",
    ) == "Wed 6:00 - 7:00 AM IDT"


def test_slot_compact_displays_add_the_google_calendar_time_when_it_differs():
    from booking_agent_cli.commands.flow import _compact_slot_displays
    from booking_agent_cli.session import Business, Session, SlotRef

    slot = SlotRef(slot_id="s", start="2026-10-13T17:00:00Z", end="2026-10-13T18:00:00Z")
    session = Session(
        business=Business(timezone="America/Phoenix"), calendar_timezone="Asia/Jerusalem",
    )
    out = _compact_slot_displays(session, slot, dual=False)
    assert out["business_range_display"].endswith("10:00 - 11:00 AM MST")
    assert out["calendar_display"] == "8:00 - 9:00 PM IDT"

    session.calendar_timezone = "America/Phoenix"
    assert "calendar_display" not in _compact_slot_displays(session, slot, dual=False)
    session.calendar_timezone = "Asia/Jerusalem"
    session.buyer_timezone = "Asia/Jerusalem"
    dual = _compact_slot_displays(session, slot, dual=True)
    assert dual["buyer_range_display"] == "8:00 - 9:00 PM IDT"
    assert "calendar_display" not in dual
