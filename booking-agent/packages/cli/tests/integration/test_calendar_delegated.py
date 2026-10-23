"""Delegated route: the agent's platform writes the calendar event and the CLI records it."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from booking_agent_cli.calendar_delegated import (
    OP_CREATE,
    delegated_request,
    is_delegated,
    record_delegated_result,
)
from booking_agent_cli.calendar_event import _load_sidecar, _save_sidecar
from booking_agent_cli.session import load_session, load_store, save_session
from integration.test_flow_gates import (
    _SERVICE_ID,
    _complete_checkout_mode_setup,
    _drive_to_payment_pending,
    _init,
    _invoke,
    _stdout_json,
)
from integration.test_resume_after_restart import _at_summary

_CONNECT = ("flow", "calendar", "connect", "--mode", "events", "--route", "delegated")
_LINK = "https://calendar.google.com/calendar/event?eid=platform-1"


def _to_gate(runner):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "ask")


def _no_token_writes(monkeypatch):
    import booking_agent_cli.commands.flow as flow

    def refuse(*_a, **_k):
        raise AssertionError("the delegated route must never write with a token")

    for name in ("create_calendar_event", "update_calendar_event", "delete_calendar_event"):
        monkeypatch.setattr(flow, name, refuse)


def _pay_poll_delegated(runner, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    _drive_to_payment_pending(runner)
    session = load_session()
    session.checkout_mode = "ucp_native"
    session.booking.checkout_path = "ucp"
    session.ucp_checkout_session_id = "chk_test"
    session.ucp_spt_network_id = "ntwrk_test"
    session.calendar_state = "connected"
    session.calendar_mode = "events"
    session.calendar_route = "delegated"
    save_session(session)

    monkeypatch.setattr(flow, "create_spend_request", lambda **_k: SpendRequestCreateResult(
        id="lsrq_poll", approval_url="https://link.test/approve",
        raw={"id": "lsrq_poll"}, command_summary="link-cli spend-request create ...",
    ))
    r = _invoke(runner, "flow", "pay", "request", "--payment-method-id", "pm_test",
                "--policies-accepted", "--context", "x" * 100)
    assert r.exit_code == 0, r.stdout + r.stderr
    monkeypatch.setattr(flow, "_poll_spend_approval_or_exit", lambda *_a, **_k: None)

    async def fake_complete(*_a, **_k):
        return {"complete_checkout": {}, "checkout_id": "chk_test", "order_id": "ord_1",
                "handler_id_resolved": "com.stripe"}

    monkeypatch.setattr(payment_helpers, "complete_ucp_checkout_async", fake_complete)
    _no_token_writes(monkeypatch)
    r = _invoke(runner, "flow", "pay", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    return _stdout_json(r)


def _record(runner, request, *, start=None, end=None, link=_LINK, event_id="platform-1", extra=()):
    event = request.get("event") or {}
    args = ["flow", "calendar", "record-event", "--request-id", request["request_id"]]
    if event_id:
        args += ["--event-id", event_id]
    if link:
        args += ["--html-link", link]
    args += ["--start", start or event["start"], "--end", end or event["end"], *extra]
    return _invoke(runner, *args)


def _utc(value: str) -> str:
    return datetime.fromisoformat(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- connecting -------------------------------------------------------------


def test_the_offer_names_the_delegated_route_for_events_only(runner, isolated_session):
    _init(runner)
    _complete_checkout_mode_setup(runner)

    prompt = _stdout_json(_invoke(runner, "flow", "calendar", "ask"))["prompt"]

    assert "--mode events --route delegated" in prompt
    assert "--mode freebusy --route delegated" not in prompt


def test_events_mode_connects_without_a_token(runner, isolated_session):
    _to_gate(runner)

    r = _invoke(runner, *_CONNECT)

    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["connected"] is True
    assert body["data"]["conflict_checking"] is False
    assert body["next_command"] == "booking-agent flow availability"
    s = load_session()
    assert (s.calendar_state, s.calendar_mode, s.calendar_route) == ("connected", "events", "delegated")
    assert s.calendar_token_env is None
    assert load_store().last_calendar_route == "delegated"


def test_free_busy_mode_is_refused(runner, isolated_session):
    _to_gate(runner)

    r = _invoke(runner, "flow", "calendar", "connect", "--mode", "freebusy", "--route", "delegated")

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "calendar_delegated_events_only"
    assert body["next_command"] == "booking-agent flow calendar skip"
    assert load_session().calendar_gate_completed is False


def test_availability_says_the_calendar_was_not_checked(runner, isolated_session):
    _to_gate(runner)
    _invoke(runner, *_CONNECT)

    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)

    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["calendar_not_checked"] is True
    assert body["data"]["calendar_filtered"] is False
    assert "not checked against the user's Google Calendar" in body["prompt"]


def test_a_late_connect_keeps_the_chosen_time(runner, isolated_session, monkeypatch):
    slot_id = _at_summary(runner, monkeypatch)

    r = _invoke(runner, *_CONNECT)

    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["phase"] == "BUYER_RESOLVED"
    assert body["next_command"] == "booking-agent flow buyer resolve"
    assert load_session().slot_id == slot_id


def test_a_saved_delegated_choice_is_reused_without_a_token(runner, isolated_session):
    _to_gate(runner)
    _invoke(runner, *_CONNECT)

    _init(runner, force=True)
    _complete_checkout_mode_setup(runner)
    body = _stdout_json(_invoke(runner, "flow", "calendar", "ask"))

    assert body["data"]["remembered_preference_applied"] is True
    assert body["awaiting_user"] != "calendar_reconnect"
    assert body["next_command"] == "booking-agent flow availability"
    assert load_session().calendar_route == "delegated"


# --- creating the event -------------------------------------------------------


def test_payment_hands_the_event_to_the_platform(runner, isolated_session, monkeypatch):
    body = _pay_poll_delegated(runner, monkeypatch)

    assert body["phase"] == "CONFIRMED"
    assert body["user_message"].startswith("You're booked")
    assert body["awaiting_user"] == "calendar_delegated_write"
    request = body["data"]["calendar"]["delegated_request"]
    assert request["operation"] == OP_CREATE
    assert body["next_command"] == request["report_command"]
    assert request["event"]["summary"] and request["event"]["start"] and request["event"]["end"]
    assert "couldn't add it" not in body["user_message"]


def test_a_matching_report_records_the_event(runner, isolated_session, monkeypatch):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]
    event = request["event"]

    r = _record(runner, request, start=_utc(event["start"]), end=_utc(event["end"]))

    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["calendar"]["status"] == "recorded"
    assert _LINK in body["prompt"]
    assert body["next_command"].startswith("booking-agent update check --after-booking")
    s = load_session()
    assert (s.calendar_event_id, s.calendar_event_html_link) == ("platform-1", _LINK)
    status = _stdout_json(_invoke(runner, "flow", "status"))
    assert _LINK in status["user_message"]
    assert status["awaiting_user"] is None


def test_a_report_with_other_times_is_refused_without_a_support_note(
    runner, isolated_session, monkeypatch,
):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]

    r = _record(runner, request, start="2030-01-01T09:00:00Z", end="2030-01-01T10:00:00Z")

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "calendar_delegated_time_mismatch"
    assert body["data"]["expected"]["start"] == request["event"]["start"]
    assert body["next_command"] == request["report_command"]
    assert body.get("support_report_offered") is not True
    assert load_session().calendar_event_id is None
    assert _load_sidecar()[load_session().id]["pending"]["request_id"] == request["request_id"]


def test_a_report_without_a_link_is_incomplete(runner, isolated_session, monkeypatch):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]

    r = _record(runner, request, link="not-a-url")

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "calendar_delegated_report_incomplete"
    assert body["data"]["missing"] == ["--html-link"]


def test_a_request_is_recorded_only_once(runner, isolated_session, monkeypatch):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]
    assert _record(runner, request).exit_code == 0

    r = _record(runner, request)

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "calendar_delegated_request_unknown"
    assert body["next_command"] == "booking-agent flow status"


def test_a_failed_write_tells_the_user_to_add_it(runner, isolated_session, monkeypatch):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]

    r = _invoke(runner, "flow", "calendar", "record-event", "--request-id", request["request_id"],
                "--outcome", "failed", "--reason", "no calendar access")

    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert "couldn't add it to your Google Calendar" in body["user_message"]
    assert body["next_command"].startswith("booking-agent update check --after-booking")
    assert load_session().id not in _load_sidecar()


def test_create_event_and_status_reuse_the_open_request(runner, isolated_session, monkeypatch):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]

    again = _stdout_json(_invoke(runner, "flow", "calendar", "create-event"))
    status = _stdout_json(_invoke(runner, "flow", "status"))

    assert again["data"]["delegated_request"]["request_id"] == request["request_id"]
    assert status["awaiting_user"] == "calendar_delegated_write"
    assert status["data"]["delegated_request"]["request_id"] == request["request_id"]
    assert status["user_message"].startswith("You're booked")


def test_create_event_after_recording_is_idempotent(runner, isolated_session, monkeypatch):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]
    _record(runner, request)

    body = _stdout_json(_invoke(runner, "flow", "calendar", "create-event"))

    data = body["data"]
    assert (data["event_id"], data["html_link"], data["idempotent"]) == ("platform-1", _LINK, True)
    assert body["awaiting_user"] is None


# --- moving and removing it ------------------------------------------------------


def test_update_event_asks_the_platform_to_move_the_recorded_event(
    runner, isolated_session, monkeypatch,
):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]
    _record(runner, request)

    body = _stdout_json(_invoke(runner, "flow", "calendar", "update-event"))

    assert body["awaiting_user"] == "calendar_delegated_write"
    move = body["data"]["delegated_request"]
    assert (move["operation"], move["event_id"]) == ("update", "platform-1")
    assert move["request_id"] != request["request_id"]
    assert _record(runner, move, event_id="platform-1").exit_code == 0


def test_delete_event_after_cancel_is_reported_back(runner, isolated_session, monkeypatch):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]
    _record(runner, request)
    session = load_session()
    session.booking.status = "canceled"
    save_session(session)

    body = _stdout_json(_invoke(runner, "flow", "calendar", "delete-event"))

    removal = body["data"]["delegated_request"]
    assert (removal["operation"], removal["event_id"]) == ("delete", "platform-1")
    assert "--event-id" not in removal["report_command"]
    r = _invoke(runner, "flow", "calendar", "record-event", "--request-id", removal["request_id"])
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["data"]["calendar"]["status"] == "deleted"
    assert session.id not in _load_sidecar()
    assert load_session().calendar_event_id is None


def test_cancel_cleanup_hands_the_delete_to_the_platform(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]
    _record(runner, request)

    result = flow._remove_calendar_event_after_cancel(load_session())

    assert result["status"] == "delegated"
    assert result["delegated_request"]["operation"] == "delete"
    assert result["delegated_request"]["report_command"] in result["agent_note"]


def test_bulk_cleanup_without_report_back_keeps_no_pending_request(
    runner, isolated_session, monkeypatch,
):
    request = _pay_poll_delegated(runner, monkeypatch)["data"]["calendar"]["delegated_request"]
    _record(runner, request)
    session = load_session()

    removal = delegated_request(session, "delete", report_back=False)

    assert removal["status"] == "requested" and "report_command" not in removal
    assert "pending" not in _load_sidecar()[session.id]


# --- the route an event was written on wins -------------------------------------


def test_an_event_keeps_the_route_it_was_written_on(isolated_session):
    from booking_agent_cli.session import Session

    session = Session(calendar_route="connector")
    _save_sidecar({session.id: {"route": "delegated", "event_id": "e1", "html_link": _LINK}})
    assert is_delegated(session) is True

    _save_sidecar({session.id: {"event_id": "e1", "html_link": _LINK}})
    session.calendar_route = "delegated"
    assert is_delegated(session) is False


def test_an_unknown_request_is_refused(isolated_session):
    assert record_delegated_result("nope", outcome="done")["code"] == "calendar_delegated_request_unknown"


# --- free/busy read: a platform that can tell the CLI its busy intervals --------


def test_connect_with_can_read_busy_sets_conflict_checking_true(runner, isolated_session):
    _to_gate(runner)

    r = _invoke(runner, *_CONNECT, "--delegated-can-read-busy")

    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["conflict_checking"] is True
    s = load_session()
    assert s.calendar_delegated_freebusy is True
    assert load_store().last_calendar_delegated_freebusy is True


def test_availability_asks_for_busy_times_before_showing_slots(runner, isolated_session):
    _to_gate(runner)
    _invoke(runner, *_CONNECT, "--delegated-can-read-busy")

    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)

    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["awaiting_user"] == "calendar_delegated_busy_request"
    request = body["data"]["delegated_busy_request"]
    assert request["window_from"] and request["window_to"]
    assert "report-busy" in request["report_command"]
    assert body["next_command"] == request["report_command"]
    # Nothing was queried or shown yet.
    assert "data" not in body or "slot_count" not in body["data"]


def test_reporting_no_busy_marks_the_calendar_checked(runner, isolated_session):
    _to_gate(runner)
    _invoke(runner, *_CONNECT, "--delegated-can-read-busy")
    ask = _stdout_json(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    request = ask["data"]["delegated_busy_request"]

    report = _invoke(
        runner, "flow", "calendar", "report-busy",
        "--request-id", request["request_id"], "--none",
    )
    assert report.exit_code == 0, report.stdout + report.stderr
    report_body = _stdout_json(report)
    assert report_body["data"]["busy_count"] == 0
    expected_next = (
        f"booking-agent flow availability --service-id {_SERVICE_ID} "
        f"--from {request['window_from']} --to {request['window_to']}"
    )
    assert report_body["next_command"] == expected_next

    final = _stdout_json(_invoke(
        runner, "flow", "availability", "--service-id", _SERVICE_ID,
        "--from", request["window_from"], "--to", request["window_to"],
    ))
    assert final["data"]["calendar_filtered"] is True
    assert final["data"]["slots_filtered"] == 0
    assert final["data"]["calendar_not_checked"] is False
    assert "not checked" not in final["prompt"]


def test_a_reported_busy_interval_removes_the_overlapping_slot(runner, isolated_session):
    # Baseline: the exact start/end of the first open slot, with no calendar connected.
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "ask")
    _invoke(runner, "flow", "calendar", "skip")
    baseline = _stdout_json(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    assert baseline["data"]["slot_count"] == 84
    first_slot = _stdout_json(_invoke(runner, "flow", "slots", "list"))["data"]["slots"][0]

    _invoke(runner, "flow", "reset")
    _init(runner, user_request="second run with delegated busy read")
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "ask")
    _invoke(runner, *_CONNECT, "--delegated-can-read-busy")

    ask = _stdout_json(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    assert ask["awaiting_user"] == "calendar_delegated_busy_request"
    request = ask["data"]["delegated_busy_request"]

    report = _invoke(
        runner, "flow", "calendar", "report-busy",
        "--request-id", request["request_id"],
        "--busy", f"{first_slot['start']},{first_slot['end']}",
    )
    assert report.exit_code == 0, report.stdout + report.stderr
    assert _stdout_json(report)["data"]["busy_count"] == 1

    final = _stdout_json(_invoke(
        runner, "flow", "availability", "--service-id", _SERVICE_ID,
        "--from", request["window_from"], "--to", request["window_to"],
    ))
    assert final["data"]["calendar_filtered"] is True
    assert final["data"]["slots_filtered"] == 1
    assert final["data"]["slot_count"] == 83


def test_a_stale_busy_request_id_is_refused(runner, isolated_session):
    _to_gate(runner)
    _invoke(runner, *_CONNECT, "--delegated-can-read-busy")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)

    r = _invoke(runner, "flow", "calendar", "report-busy", "--request-id", "nope", "--none")

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "calendar_delegated_busy_request_unknown"
    assert body["next_command"] == "booking-agent flow availability"


def test_a_busy_report_needs_busy_or_none(runner, isolated_session):
    _to_gate(runner)
    _invoke(runner, *_CONNECT, "--delegated-can-read-busy")
    ask = _stdout_json(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    request = ask["data"]["delegated_busy_request"]

    r = _invoke(runner, "flow", "calendar", "report-busy", "--request-id", request["request_id"])

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "calendar_delegated_busy_report_incomplete"


def test_a_saved_can_read_busy_choice_is_reused_and_still_asked_per_window(
    runner, isolated_session,
):
    _to_gate(runner)
    _invoke(runner, *_CONNECT, "--delegated-can-read-busy")

    _init(runner, force=True)
    _complete_checkout_mode_setup(runner)
    ask = _stdout_json(_invoke(runner, "flow", "calendar", "ask"))

    assert ask["data"]["remembered_preference_applied"] is True
    assert load_session().calendar_delegated_freebusy is True

    availability = _stdout_json(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    assert availability["awaiting_user"] == "calendar_delegated_busy_request"
