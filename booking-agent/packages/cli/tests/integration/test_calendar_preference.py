"""Tests for remembered calendar preference persistence."""
from booking_agent.calendar.scope import CALENDAR_MODE_EVENTS, CALENDAR_MODE_NONE
from booking_agent_cli.calendar_preference import (
    get_calendar_preference,
    remembered_preference_applicable,
    save_calendar_preference,
    try_apply_remembered_calendar_gate,
)
from booking_agent_cli.session import Business, Session, add_session, load_store, save_store


def _seed(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    store = load_store()
    session = Session(
        flow_phase="BUSINESS_SELECTED",
        business=Business(profile_url="https://example.com/profile", name="Salon"),
        checkout_prereq_completed=True,
    )
    add_session(store, session)
    save_store(store)
    return session


def test_save_and_load_preference(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    save_calendar_preference(state="connected", mode=CALENDAR_MODE_EVENTS)
    assert get_calendar_preference() == ("connected", CALENDAR_MODE_EVENTS)


def test_remembered_preference_applicable_requires_connection(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    save_calendar_preference(state="connected", mode=CALENDAR_MODE_EVENTS)
    assert remembered_preference_applicable({"connected": False}) is False
    assert remembered_preference_applicable(
        {
            "connected": True,
            "scope": (
                "https://www.googleapis.com/auth/calendar.events "
                "https://www.googleapis.com/auth/calendar.freebusy"
            ),
            "supports_freebusy": True,
            "supports_events": True,
        }
    ) is True


def test_try_apply_skipped_preference_when_connected(tmp_path, monkeypatch):
    session = _seed(tmp_path, monkeypatch)
    save_calendar_preference(state="skipped", mode=CALENDAR_MODE_NONE)
    result = try_apply_remembered_calendar_gate(
        session,
        {"connected": True, "supports_freebusy": True, "supports_events": True},
    )
    assert result is not None
    assert result.state == "skipped"
    assert session.calendar_gate_completed is True


def test_try_apply_infers_events_mode_when_connected_without_saved_preference(
    tmp_path, monkeypatch,
):
    session = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "booking_agent_cli.calendar_preference.bridge_token_cache_for_mode",
        lambda mode, user_key="cli": True,
    )
    result = try_apply_remembered_calendar_gate(
        session,
        {
            "connected": True,
            "scope": (
                "https://www.googleapis.com/auth/calendar.events "
                "https://www.googleapis.com/auth/calendar.freebusy"
            ),
            "supports_freebusy": True,
            "supports_events": True,
        },
    )
    assert result is not None
    assert result.mode == CALENDAR_MODE_EVENTS
    assert session.calendar_gate_completed is True
    assert get_calendar_preference() == ("connected", CALENDAR_MODE_EVENTS)


def test_stale_remembered_mode_when_connected_pref_lost_connection(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_preference import stale_remembered_calendar_mode

    _seed(tmp_path, monkeypatch)
    save_calendar_preference(state="connected", mode=CALENDAR_MODE_EVENTS)
    assert stale_remembered_calendar_mode({"connected": False}) == CALENDAR_MODE_EVENTS
    assert stale_remembered_calendar_mode({"connected": True}) is None


def test_stale_remembered_mode_ignores_skip_and_missing_pref(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_preference import stale_remembered_calendar_mode

    _seed(tmp_path, monkeypatch)
    assert stale_remembered_calendar_mode({"connected": False}) is None
    save_calendar_preference(state="skipped", mode=CALENDAR_MODE_NONE)
    assert stale_remembered_calendar_mode({"connected": False}) is None
