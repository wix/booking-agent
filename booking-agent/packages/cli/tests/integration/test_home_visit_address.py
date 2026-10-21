"""Home visits: the confirmed street address is the only address sent at booking."""
from __future__ import annotations

import pytest

import booking_agent_cli.geocode as geo
from booking_agent_cli.session import load_session, must_load, save_session
from support.mock.link_cli import YUMA_SHIPPING_ADDRESS, mock_link_cli_user_info
from integration.test_flow_gates import (
    _PROFILE_URL,
    _SERVICE_ID,
    _init,
    _invoke,
    _satisfy_link_auth_gate,
    _stdout_json,
)

_BUYER = {"name": "Ada Lovelace", "email": "ada@booking-agent.test"}


@pytest.fixture
def home_visit(isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent.models.wire import Channel, ChannelType

    backend = flow.resolve_backend()
    original_get_service = backend.get_service

    async def at_buyer(profile, service_id):
        service = await original_get_service(profile, service_id)
        return service.model_copy(update={
            "channel": Channel(type=ChannelType.AT_BUYER_LOCATION), "locations": None,
        })

    monkeypatch.setattr(backend, "get_service", at_buyer)
    monkeypatch.setattr(
        geo, "_geocode_http",
        lambda _p: [{"lat": "32.6927", "lon": "-114.6277", "display_name": "Yuma"}],
    )
    mock_link_cli_user_info(monkeypatch, _BUYER, shipping_addresses=[YUMA_SHIPPING_ADDRESS])

    captured = {}
    create = payment_helpers._create_booking_via_ucp_checkout

    async def capture(session, service_id, notes, delivery_address=None):
        captured.update(notes=notes, delivery_address=delivery_address)
        return await create(session, service_id, notes, delivery_address=delivery_address)

    monkeypatch.setattr(payment_helpers, "_create_booking_via_ucp_checkout", capture)
    return captured


def _business_use(runner):
    return _invoke(
        runner, "flow", "business", "use", "--profile-url", _PROFILE_URL, "--service-id", _SERVICE_ID,
    )


def _to_buyer_resolved(runner, *resolve_args):
    for cmd in (
        ["flow", "use-ucp-checkout-mode"],
        ["flow", "calendar", "ask"],
        ["flow", "calendar", "skip"],
        ["flow", "availability", "--service-id", _SERVICE_ID],
    ):
        r = _invoke(runner, *cmd)
        assert r.exit_code == 0, r.stdout
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    assert _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed").exit_code == 0
    _satisfy_link_auth_gate()
    r = _invoke(runner, "flow", "buyer", "resolve", *resolve_args)
    assert r.exit_code == 0, r.stdout
    return _stdout_json(r)


def test_business_use_asks_for_the_service_address_then_book_sends_it(
    runner, home_visit, monkeypatch,
):
    _init(runner)
    body = _stdout_json(_business_use(runner))
    assert body["awaiting_user"] == "service_address_confirmation"
    assert body["data"]["proposed_area"]["label"] == "100 Test Street, Yuma AZ 85364"
    assert load_session().business.profile_url is None

    confirmed = _stdout_json(
        _invoke(runner, "flow", "search-area", "confirm", "--user-confirmed"),
    )
    assert confirmed["next_command"].startswith("booking-agent flow business use --profile-url")

    r = _business_use(runner)
    assert r.exit_code == 0, r.stdout
    assert _stdout_json(r)["next_command"] == "booking-agent flow use-ucp-checkout-mode"

    _to_buyer_resolved(runner)
    assert load_session().buyer_timezone == "America/Phoenix"
    booked = _invoke(runner, "flow", "book", "--user-confirmed", "--policies-accepted")
    assert booked.exit_code == 0, booked.stdout
    address = home_visit["delivery_address"]
    assert address["line1"] == "100 Test Street"
    assert address["city"] == "Yuma"
    assert address["country"] == "US"
    assert address["coordinates"] == {"lat": 32.6927, "lng": -114.6277}


def test_book_refuses_a_home_visit_without_a_confirmed_address(runner, home_visit, monkeypatch):
    _init(runner)
    _business_use(runner)
    _invoke(runner, "flow", "search-area", "confirm", "--user-confirmed")
    _business_use(runner)
    _to_buyer_resolved(runner)
    session = must_load()
    session.service_address = None
    save_session(session)

    r = _invoke(runner, "flow", "book", "--user-confirmed", "--policies-accepted")
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "service_address_unconfirmed"
    assert body["next_command"] == "booking-agent flow search-area propose --purpose service-address"
    assert load_session().area_resume_command == "booking-agent flow book"
    assert "delivery_address" not in home_visit


def test_recap_lines_echo_every_detail_and_book_sends_the_note(runner, home_visit):
    _init(runner)
    _business_use(runner)
    _invoke(runner, "flow", "search-area", "confirm", "--user-confirmed")
    _business_use(runner)
    recap = _to_buyer_resolved(
        runner, "--notes", "Window AC unit,\n completely unresponsive, brand unknown",
    )
    lines = recap["data"]["booking_recap_lines"]
    assert lines[0].startswith("Service: ")
    assert lines[1].startswith("When: ") and " - " in lines[1]
    assert "Where: At your address, 100 Test Street, Yuma AZ 85364" in lines
    assert any(line.startswith("Price: ") and "Link wallet" in line for line in lines)
    assert "Name: Ada Lovelace" in lines
    assert "Email: ada@booking-agent.test" in lines
    assert "Note for the business: Window AC unit, completely unresponsive, brand unknown" in lines
    assert "booking_recap_lines verbatim" in recap["prompt"]

    assert _invoke(runner, "flow", "book", "--user-confirmed", "--policies-accepted").exit_code == 0
    assert home_visit["notes"] == "Window AC unit, completely unresponsive, brand unknown"
