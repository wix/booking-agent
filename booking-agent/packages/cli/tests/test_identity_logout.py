"""Consumer identity logout (Link + Google Calendar)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from booking_agent_cli.identity import logout_identity
from booking_agent_cli.main import app
from booking_agent_cli.session import (
    BookingRef,
    Business,
    Session,
    SessionStore,
    SlotRef,
    add_session,
    load_store,
    save_store,
)


def test_logout_clears_calendar_cache_and_keeps_booking(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    cache_dir = tmp_path / "google-calendar-tokens"
    cache_dir.mkdir()
    token_file = cache_dir / "abc.json"
    token_file.write_text('{"access_token":"tok"}', encoding="utf-8")
    monkeypatch.setattr(
        "booking_agent_cli.identity.google_calendar_token_cache_dir",
        lambda: cache_dir,
    )

    store = SessionStore()
    start = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    session = Session(
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start, end=end)],
        booking=BookingRef(id="bk_keep", credential="bsc_still_here"),
        business=Business(name="Salon"),
        link_auth_gate_completed=True,
        calendar_gate_completed=True,
        calendar_state="connected",
    )
    add_session(store, session)
    store.last_calendar_state = "connected"
    store.last_calendar_mode = "events"
    save_store(store)

    monkeypatch.setattr("booking_agent_cli.link_cli.logout", lambda: None)

    result = logout_identity()
    assert result["link_cleared"] is True
    assert not token_file.exists()
    assert result["upcoming_sessions_kept"] == 1

    reloaded = load_store()
    active = reloaded.sessions[-1]
    assert active.booking.credential == "bsc_still_here"
    assert active.link_auth_gate_completed is False
    assert reloaded.last_calendar_state is None


def test_logout_reports_env_calendar_token(monkeypatch, tmp_path):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("GOOGLECALENDAR_ACCESS_TOKEN", "env-tok")
    monkeypatch.setattr("booking_agent_cli.link_cli.logout", lambda: None)
    result = logout_identity()
    assert result["calendar_env_token_still_present"] is True


def test_flow_logout_command(runner, isolated_session, monkeypatch):
    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.logout_identity",
        lambda: {
            "link_cleared": True,
            "google_cache_cleared": [],
            "calendar_preference_cleared": True,
            "calendar_env_token_still_present": False,
            "upcoming_sessions_kept": 0,
        },
    )
    r = runner.invoke(app, ["flow", "logout"])
    assert r.exit_code == 0
    body = json.loads(r.stdout.strip())
    assert body["data"]["link_cleared"] is True
