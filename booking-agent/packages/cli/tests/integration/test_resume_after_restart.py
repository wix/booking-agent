"""A booking that was waiting for the user's yes survives a restart and a stale support note.

Hosted replay of 0.9.0: after a new conversation the assistant saw only a support-report
question, read the user's "yes" as the answer to it, and re-listed times, which dropped
the confirmed slot and summary.
"""
from __future__ import annotations

import booking_agent_cli.support_report as sr
from integration.test_flow_gates import (
    _SERVICE_ID,
    _complete_checkout_mode_setup,
    _init,
    _invoke,
    _satisfy_link_auth_gate,
    _skip_calendar,
    _stdout_json,
)
from support.mock.link_cli import mock_link_cli_user_info


def _at_summary(runner, monkeypatch) -> str:
    """Drive a booking to BUYER_RESOLVED (the summary waits for the user's yes)."""
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _skip_calendar(runner)
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    mock_link_cli_user_info(
        monkeypatch,
        {"name": "Ada Lovelace", "email": "ada@booking-agent.test", "phone": "+15551234567"},
    )
    body = _stdout_json(_invoke(runner, "flow", "buyer", "resolve"))
    assert body["phase"] == "BUYER_RESOLVED"
    return slot_id


def _queue_note(phase: str, code: str = "internal_error") -> None:
    from booking_agent_cli.session import load_session, save_session

    session = load_session()
    assert sr.maybe_offer(session, code, None, phase)
    save_session(session, write_support_report=True)


def test_a_forgotten_service_id_does_not_queue_a_support_note(runner, isolated_session):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _skip_calendar(runner)

    r = _invoke(runner, "flow", "availability")

    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "missing_service_id"
    assert out.get("support_report_offered") is not True
    assert out["next_command"], "the agent needs a command to recover with"
    from booking_agent_cli.session import load_session

    assert not load_session().support_report_bytes


def test_status_shows_the_booking_when_the_note_is_from_an_earlier_step(
    runner, isolated_session, monkeypatch,
):
    _at_summary(runner, monkeypatch)
    _queue_note("CALENDAR_GATE")

    out = _stdout_json(_invoke(runner, "flow", "status"))

    assert out["phase"] == "BUYER_RESOLVED"
    assert out["awaiting_user"] != sr.AWAITING_SUPPORT_REPORT_CONSENT
    assert out["next_command"] != sr.SEND_COMMAND
    from booking_agent_cli.session import load_session

    assert not load_session().support_report_bytes


def test_status_still_asks_about_a_note_from_the_current_step(
    runner, isolated_session, monkeypatch,
):
    _at_summary(runner, monkeypatch)
    _queue_note("BUYER_RESOLVED")

    out = _stdout_json(_invoke(runner, "flow", "status"))

    assert out["awaiting_user"] == sr.AWAITING_SUPPORT_REPORT_CONSENT


def test_availability_cannot_undo_a_confirmed_slot(runner, isolated_session, monkeypatch):
    slot_id = _at_summary(runner, monkeypatch)

    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)

    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "availability_after_slot_confirmed"
    from booking_agent_cli.session import load_session

    session = load_session()
    assert session.flow_phase == "BUYER_RESOLVED"
    assert session.slot_id == slot_id


def test_availability_can_change_the_time_when_asked(runner, isolated_session, monkeypatch):
    _at_summary(runner, monkeypatch)

    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID, "--change-time")

    assert r.exit_code == 0, r.stdout
    assert _stdout_json(r)["phase"] == "AVAILABILITY_QUERIED"


def _fresh_runtime(monkeypatch):
    """The booking file is kept, the runtime's temporary folder is not."""
    import booking_agent_cli.runtime_prepare as rp
    from booking_agent_cli.session import session_path

    monkeypatch.setenv("BOOKING_AGENT_RESUME_CHECK", "1")
    rp._resume_marker(session_path()).unlink(missing_ok=True)


def test_a_new_runtime_must_read_status_before_changing_the_booking(
    runner, isolated_session, monkeypatch,
):
    slot_id = _at_summary(runner, monkeypatch)
    _fresh_runtime(monkeypatch)

    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID, "--change-time")

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "resume_check_required"
    assert out["next_command"] == "booking-agent flow status"
    assert out.get("support_report_offered") is not True
    from booking_agent_cli.session import load_session

    assert load_session().slot_id == slot_id


def test_status_opens_the_way_for_the_rest_of_the_runtime(runner, isolated_session, monkeypatch):
    _at_summary(runner, monkeypatch)
    _fresh_runtime(monkeypatch)

    status = _stdout_json(_invoke(runner, "flow", "status"))
    assert status["phase"] == "BUYER_RESOLVED"
    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID, "--change-time")

    assert r.exit_code == 0, r.stdout


def test_the_check_can_be_turned_off(runner, isolated_session, monkeypatch):
    _at_summary(runner, monkeypatch)
    _fresh_runtime(monkeypatch)
    monkeypatch.setenv("BOOKING_AGENT_RESUME_CHECK", "0")

    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID, "--change-time")

    assert r.exit_code == 0, r.stdout


def test_a_note_question_does_not_need_status_first(runner, isolated_session, monkeypatch):
    _at_summary(runner, monkeypatch)
    _queue_note("BUYER_RESOLVED")
    _fresh_runtime(monkeypatch)

    r = _invoke(runner, "flow", "support-report")

    assert _stdout_json(r)["awaiting_user"] == sr.AWAITING_SUPPORT_REPORT_CONSENT


def _booked_text(session) -> str:
    from booking_agent_cli.booking_summary import booked_message

    return booked_message(session)


def test_the_booked_message_says_when_the_calendar_was_skipped_unasked(
    runner, isolated_session, monkeypatch,
):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "ask")
    r = _invoke(runner, "flow", "calendar", "skip", "--session-only", "--no-route")
    assert r.exit_code == 0, r.stdout
    from booking_agent_cli.session import load_session

    session = load_session()
    assert session.calendar_skipped_unasked is True
    assert "No calendar event was added" in _booked_text(session)


def test_a_calendar_the_user_declined_is_not_mentioned_again(runner, isolated_session):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "ask")
    _invoke(runner, "flow", "calendar", "skip", "--session-only")
    from booking_agent_cli.session import load_session

    session = load_session()
    assert session.calendar_skipped_unasked is False
    assert "calendar" not in _booked_text(session).lower()


def test_each_new_runtime_is_counted_beside_the_saved_booking(isolated_session):
    import booking_agent_cli.runtime_prepare as rp
    from booking_agent_cli.session import session_path

    path = session_path()
    rp.mark_runtime_seen(path)
    rp.mark_runtime_seen(path)
    assert rp.runtime_starts(path.parent) == 1

    rp._resume_marker(path).unlink()
    rp.mark_runtime_seen(path)
    assert rp.runtime_starts(path.parent) == 2


def test_doctor_says_why_a_restart_is_not_shown(isolated_session, monkeypatch, tmp_path):
    import booking_agent_cli.runtime_prepare as rp

    keep = tmp_path / "keep"
    keep.mkdir()
    monkeypatch.setattr(rp, "current_boot_id", lambda: "boot-a")
    monkeypatch.setattr(
        rp, "_describe", lambda p: {"path": str(p), "linked_to": str(keep / "booking-agent"), "exists": True},
    )
    persistence = rp.diagnose()["persistence"]
    assert persistence["survived_restart"] is None
    assert persistence["survived_restart_basis"] == "no_boot_marker_yet"

    from booking_agent_cli.session import session_path

    rp.mark_runtime_seen(session_path())
    rp._resume_marker(session_path()).unlink()
    rp.mark_runtime_seen(session_path())
    persistence = rp.diagnose()["persistence"]
    assert persistence["survived_restart"] is True
    assert persistence["survived_restart_basis"] == "second_runtime_read_the_saved_booking"
