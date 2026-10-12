"""Tests for session activity logging."""
from __future__ import annotations

import json
from io import StringIO

from booking_agent_cli.activity_log import (
    MAX_ACTIVITY_LOG,
    format_argv,
    record_command,
    record_error,
)
from booking_agent_cli.output import emit_error
from booking_agent_cli.session import Session, load_session, save_session, session_path


def test_record_command_appends_and_clears_last_error(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    session = Session(user_request="book haircut")
    record_error(session, "gate", "blocked")
    record_command(session, "booking-agent flow discover --query haircut")

    assert len(session.activity_log) == 2
    assert session.activity_log[-1].kind == "command"
    assert session.activity_log[-1].text == "booking-agent flow discover --query haircut"
    assert session.last_error is None


def test_record_error_sets_last_error_and_log_entry():
    session = Session()
    record_error(session, "slot_not_selected", "pick a slot")

    assert session.last_error is not None
    assert session.last_error.code == "slot_not_selected"
    assert session.activity_log[-1].kind == "error"
    assert session.activity_log[-1].code == "slot_not_selected"


def test_activity_log_trimmed_to_max():
    session = Session()
    for i in range(MAX_ACTIVITY_LOG + 5):
        record_command(session, f"booking-agent flow status #{i}")
    assert len(session.activity_log) == MAX_ACTIVITY_LOG
    assert session.activity_log[0].text == "booking-agent flow status #5"


def test_emit_error_persists_to_active_session(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    session = Session(user_request="demo")
    save_session(session)

    stream = StringIO()
    emit_error(
        "INIT",
        code="no_business",
        message="Run discover first",
        stream=stream,
    )

    reloaded = load_session()
    assert reloaded is not None
    assert reloaded.last_error is not None
    assert reloaded.last_error.code == "no_business"
    assert any(e.kind == "error" for e in reloaded.activity_log)


def test_activity_log_round_trips_through_session_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    session = Session(user_request="demo")
    record_command(session, "booking-agent flow init --user-request demo")
    record_error(session, "discover_failed", "registry down")
    save_session(session)

    raw = json.loads(session_path().read_text())
    active_id = raw["active_session_id"]
    stored = next(s for s in raw["sessions"] if s["id"] == active_id)
    assert len(stored["activity_log"]) == 2
    assert stored["activity_log"][0]["kind"] == "command"
    assert stored["activity_log"][1]["kind"] == "error"

    reloaded = load_session()
    assert reloaded is not None
    assert len(reloaded.activity_log) == 2


def test_format_argv_joins_shell_words():
    assert format_argv(["booking-agent", "flow", "discover", "--query", "hair cut"]) == (
        "booking-agent flow discover --query 'hair cut'"
    )


def test_activity_log_redacts_address_notes_and_context_values():
    from booking_agent_cli.activity_log import format_argv

    text = format_argv([
        "booking-agent", "flow", "search-area", "set",
        "--address", "3194 West Rome Street, Yuma AZ", "--purpose", "service_address",
    ])
    assert "Rome" not in text and "--address '<redacted>'" in text
    assert "--purpose service_address" in text
    text = format_argv(["booking-agent", "flow", "buyer", "resolve", "--notes=Gate code 4417"])
    assert "4417" not in text and "'--notes=<redacted>'" in text
    text = format_argv(["booking-agent", "flow", "pay", "request", "--context", "secret plan"])
    assert "secret" not in text
