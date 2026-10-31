"""Booking for someone else: the USP ``recipient``, taken only from what the user typed."""
import json

from support.fixtures.services import set_zero_price_free_service
from support.mock import backend as mock_backend
from support.mock.link_cli import YUMA_SHIPPING_ADDRESS, mock_link_cli_user_info
from integration.test_flow_gates import (
    _BOOK,
    _BUYER,
    _SERVICE_ID,
    _YUMA,
    _business_use,
    _geocoder,
    _init,
    _invoke,
    _satisfy_link_auth_gate,
    _skip_calendar,
    _stdout_json,
)


def _ok(result):
    assert result.exit_code == 0, result.stdout + result.stderr
    return _stdout_json(result)


def _err(result, code):
    assert result.exit_code == 1, result.stdout + result.stderr
    body = _stdout_json(result)
    assert body["data"]["error"]["code"] == code, body
    return body


def _to_slot(runner, monkeypatch):
    set_zero_price_free_service(monkeypatch, _SERVICE_ID)
    mock_link_cli_user_info(monkeypatch, _BUYER, shipping_addresses=[YUMA_SHIPPING_ADDRESS])
    _geocoder(monkeypatch, _YUMA)
    _init(runner)
    _ok(_business_use(runner))
    _ok(_skip_calendar(runner))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    slots = _ok(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _ok(_invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed"))
    _satisfy_link_auth_gate()


def _set(runner, *names):
    return _invoke(runner, "flow", "recipient", "set", "--first-name", names[0], "--last-name", names[1])


def test_recipient_is_sent_on_create_shown_in_the_summary_and_warned_when_not_echoed(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.calendar_event import build_event_body
    from booking_agent_cli.session import must_load

    _to_slot(runner, monkeypatch)
    _ok(_set(runner, "Noa", "Levi"))
    recap = _ok(_invoke(runner, "flow", "buyer", "resolve"))
    lines = recap["data"]["booking_recap_lines"]
    assert "For: Noa Levi. Booked by: Ada Lovelace" in lines
    assert not any(line.startswith("Name:") for line in lines)

    booked = _ok(_invoke(runner, *_BOOK))
    [create] = [kw for m, kw in mock_backend.BQ_CALLS if m == "create_booking"]
    assert create["recipient"] == {"first_name": "Noa", "last_name": "Levi"}
    assert booked["data"]["warnings"][0]["code"] == "recipient_not_echoed"
    assert "under your name" in booked["data"]["booking_narration"]
    body, _ = build_event_body(must_load())
    assert "For: Noa Levi. Booked by: Ada Lovelace" in body["description"]


def test_changing_the_recipient_after_the_summary_needs_a_new_yes(
    runner, isolated_session, monkeypatch,
):
    _to_slot(runner, monkeypatch)
    _ok(_set(runner, "Noa", "Levi"))
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    _ok(_set(runner, "Tom", "Levi"))
    _err(_invoke(runner, *_BOOK), "booking_confirmation_required")
    recap = _ok(_invoke(runner, "flow", "buyer", "resolve"))
    assert "For: Tom Levi. Booked by: Ada Lovelace" in recap["data"]["booking_recap_lines"]


def test_cleared_recipient_is_not_sent(runner, isolated_session, monkeypatch):
    _to_slot(runner, monkeypatch)
    _ok(_set(runner, "Noa", "Levi"))
    _ok(_invoke(runner, "flow", "recipient", "clear"))
    recap = _ok(_invoke(runner, "flow", "buyer", "resolve"))
    assert "Name: Ada Lovelace" in recap["data"]["booking_recap_lines"]
    booked = _ok(_invoke(runner, *_BOOK))
    assert mock_backend.BQ_CALLS == []
    assert "warnings" not in booked["data"]


def test_the_buyer_is_not_a_recipient(runner, isolated_session, monkeypatch):
    _to_slot(runner, monkeypatch)
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    _err(_set(runner, "Ada", "Lovelace"), "recipient_is_buyer")


def test_after_booking_the_recipient_changes_only_through_update_with_a_yes(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import must_load

    _to_slot(runner, monkeypatch)
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    _ok(_invoke(runner, *_BOOK))
    _err(_set(runner, "Noa", "Levi"), "recipient_locked")

    ask = _ok(_invoke(runner, "flow", "book", "update-recipient",
                      "--first-name", "Noa", "--last-name", "Levi"))
    assert ask["awaiting_user"] == "recipient_update_confirmation"
    assert [m for m, _ in mock_backend.BQ_CALLS] == []
    other = _ok(_invoke(runner, "flow", "book", "update-recipient", "--first-name", "Tom",
                        "--last-name", "Levi", "--user-confirmed"))
    assert other["awaiting_user"] == "recipient_update_confirmation"
    assert [m for m, _ in mock_backend.BQ_CALLS] == []
    ask = _ok(_invoke(runner, "flow", "book", "update-recipient",
                      "--first-name", "Noa", "--last-name", "Levi"))
    done = _ok(_invoke(runner, *ask["next_command"].split()[1:]))
    [update] = [kw for m, kw in mock_backend.BQ_CALLS if m == "update_booking"]
    assert update["recipient"] == {"first_name": "Noa", "last_name": "Levi"}
    assert "now for Noa Levi" in done["user_message"]
    assert must_load().recipient["first_name"] == "Noa"


def test_older_session_without_recipient_loads(isolated_session):
    from booking_agent_cli.session import _session_from_dict

    raw = {"id": "s1", "flow_phase": "INIT"}
    session = _session_from_dict(raw)
    assert session.recipient is None
    assert session.recipient_pending is None
    assert session.booking_questions == {}
    json.dumps(session.booking_questions)
