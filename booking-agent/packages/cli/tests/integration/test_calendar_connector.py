"""Connector route: the agent's platform supplies a Google Calendar token in a variable the agent names."""
import json
from pathlib import Path

import pytest

from booking_agent_cli.main import app
from booking_agent_cli.session import (
    Business,
    Session,
    SlotRef,
    add_session,
    load_store,
    must_load,
    save_session,
    save_store,
)

# Deliberately not a name any platform uses: the CLI must work with whatever name it is given.
_VAR = "SOME_PLATFORM_CAL_TOKEN_7"
_TOKEN = "ya29.host-connector-secret"
_FREEBUSY = "https://www.googleapis.com/auth/calendar.freebusy"
_EVENTS = "https://www.googleapis.com/auth/calendar.events"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(_VAR, raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN_CACHE_DIR", raising=False)
    return tmp_path


def _seed(*, phase="BUSINESS_SELECTED", calendar_state="unset") -> Session:
    store = load_store()
    session = Session(
        flow_phase=phase,
        user_request="book a haircut",
        business=Business(profile_url="https://example.com/profile", name="Salon"),
        checkout_prereq_completed=True,
        calendar_gate_completed=calendar_state != "unset",
        calendar_state=calendar_state,
        calendar_ask_surfaced=True,
        service_id="svc-1",
    )
    add_session(store, session)
    save_store(store)
    return session


def _last_json(result):
    dec = json.JSONDecoder()
    s, idx, last = result.stdout.strip(), 0, None
    while idx < len(s):
        while idx < len(s) and s[idx].isspace():
            idx += 1
        if idx >= len(s):
            break
        last, idx = dec.raw_decode(s, idx)
    assert last is not None, result.stdout
    return last


def _tokeninfo(monkeypatch, scope, seen=None):
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"scope": scope, "expires_in": 3600}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            if seen is not None:
                seen.append((params or {}).get("access_token"))
            return FakeResp()

    monkeypatch.setattr("booking_agent.calendar.scope.httpx.Client", FakeClient)


def _connect(runner, mode, *extra):
    return runner.invoke(app, ["flow", "calendar", "connect", "--mode", mode, *extra])


def _connector(runner, mode, var=_VAR):
    return _connect(runner, mode, "--route", "connector", "--token-env", var)


def _assert_token_never_stored(result, home):
    assert _TOKEN not in result.stdout
    assert _TOKEN not in (result.stderr or "")
    for path in Path(home).rglob("*"):
        if path.is_file() and ".google-calendar-tokens" not in path.parts:
            assert _TOKEN not in path.read_text(errors="ignore"), path
    for env_file in Path(home).rglob(".env"):
        assert _TOKEN not in env_file.read_text()


def test_connect_without_a_route_refuses_to_guess(runner, home):
    _seed()
    result = _connect(runner, "freebusy")
    assert result.exit_code == 1
    body = _last_json(result)
    assert body["data"]["error"]["code"] == "calendar_route_required"
    assert body["next_command"] is None
    assert must_load().calendar_gate_completed is False


@pytest.mark.parametrize("extra", [[], ["--token-env", "1NOT-A-NAME"]])
def test_connector_route_needs_a_valid_variable_name(runner, home, extra):
    _seed()
    result = _connect(runner, "freebusy", "--route", "connector", *extra)
    assert result.exit_code == 1
    assert _last_json(result)["data"]["error"]["code"] == "calendar_token_env_required"


def test_missing_token_asks_the_agent_to_connect_through_its_platform(runner, home):
    _seed()
    result = _connector(runner, "events")
    assert result.exit_code == 0
    body = _last_json(result)
    assert body["awaiting_user"] == "calendar_connector"
    assert body["data"]["error"] == "calendar_not_connected"
    assert "add events" in body["data"]["scope_request"]
    assert body["data"]["retry_command"] == (
        f"booking-agent flow calendar connect --mode events --route connector --token-env {_VAR}"
    )
    assert body["next_command"] == body["data"]["retry_command"]
    assert "localhost" not in result.stdout and "auth_url" not in body["data"]
    assert must_load().calendar_gate_completed is False


def test_an_empty_variable_counts_as_not_connected(runner, home, monkeypatch):
    _seed()
    monkeypatch.setenv(_VAR, "   ")
    body = _last_json(_connector(runner, "freebusy"))
    assert body["awaiting_user"] == "calendar_connector"


def test_a_token_from_an_env_file_is_ignored(runner, home):
    _seed()
    (Path(home) / ".env").write_text(f"{_VAR}={_TOKEN}\n")
    body = _last_json(_connector(runner, "freebusy"))
    assert body["awaiting_user"] == "calendar_connector"


def test_events_mode_with_free_busy_only_access_is_insufficient(runner, home, monkeypatch):
    _seed()
    monkeypatch.setenv(_VAR, _TOKEN)
    _tokeninfo(monkeypatch, _FREEBUSY)
    result = _connector(runner, "events")
    assert result.exit_code == 1
    body = _last_json(result)
    assert body["data"]["error"]["code"] == "calendar_insufficient_scope"
    assert "add events" in body["data"]["scope_request"]
    assert "localhost" not in result.stdout
    _assert_token_never_stored(result, home)


def test_free_busy_access_completes_free_busy_mode(runner, home, monkeypatch):
    _seed()
    monkeypatch.setenv(_VAR, _TOKEN)
    _tokeninfo(monkeypatch, _FREEBUSY)
    result = _connector(runner, "freebusy")
    assert result.exit_code == 0, result.stdout
    body = _last_json(result)
    assert body["next_command"] == "booking-agent flow availability"
    assert body["data"]["connected"] is True
    s = must_load()
    assert (s.calendar_state, s.calendar_mode) == ("connected", "freebusy")


def test_write_access_completes_events_mode_and_caches_privately(runner, home, monkeypatch):
    import stat

    from booking_agent_cli.identity import google_calendar_token_cache_dir

    _seed()
    monkeypatch.setenv(_VAR, _TOKEN)
    _tokeninfo(monkeypatch, f"{_EVENTS} {_FREEBUSY}")
    result = _connector(runner, "events")
    assert result.exit_code == 0, result.stdout

    s = must_load()
    assert (s.calendar_state, s.calendar_mode) == ("connected", "events")
    assert (s.calendar_route, s.calendar_token_env) == ("connector", _VAR)
    store = load_store()
    assert (store.last_calendar_route, store.last_calendar_token_env) == ("connector", _VAR)
    cache_dir = google_calendar_token_cache_dir()
    files = list(cache_dir.glob("*.json"))
    assert files and all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)
    _assert_token_never_stored(result, home)


def test_the_stored_variable_name_is_reused_by_later_bookings(runner, home, monkeypatch):
    _seed()
    monkeypatch.setenv(_VAR, _TOKEN)
    _tokeninfo(monkeypatch, f"{_EVENTS} {_FREEBUSY}")
    assert _connector(runner, "events").exit_code == 0

    _seed()
    result = runner.invoke(app, ["flow", "calendar", "ask"])
    body = _last_json(result)
    assert body["data"]["remembered_preference_applied"] is True
    assert body["next_command"] == "booking-agent flow availability"


def test_a_lost_connector_token_offers_a_connector_reconnect(runner, home, monkeypatch):
    _seed()
    monkeypatch.setenv(_VAR, _TOKEN)
    _tokeninfo(monkeypatch, f"{_EVENTS} {_FREEBUSY}")
    assert _connector(runner, "events").exit_code == 0

    from booking_agent_cli.calendar_event import _user_cache_path

    Path(_user_cache_path("cli")).unlink()
    monkeypatch.delenv(_VAR)
    _seed()
    body = _last_json(runner.invoke(app, ["flow", "calendar", "ask"]))
    assert body["awaiting_user"] == "calendar_reconnect"
    assert body["next_command"] == (
        f"booking-agent flow calendar connect --mode events --route connector --token-env {_VAR}"
    )


def test_event_writes_refresh_the_cache_from_the_current_host_token(runner, home, monkeypatch):
    from booking_agent_cli.calendar_event import load_cached_token, write_token_cache

    session = _seed(phase="CONFIRMED", calendar_state="connected")
    session.calendar_mode = "events"
    session.calendar_route = "connector"
    session.calendar_token_env = _VAR
    session.slot_id = "slot-1"
    session.slots_presented = [
        SlotRef(slot_id="slot-1", start="2026-07-10T10:00:00Z", end="2026-07-10T11:00:00Z")
    ]
    session.live_service = {"title": "Haircut"}
    save_session(session)
    write_token_cache("cli", "ya29.rotated-out", expires_in=3600)
    monkeypatch.setenv(_VAR, _TOKEN)
    sent = []

    class FakeResp:
        status_code = 200

        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class FakeClient:
        """tokeninfo and the Calendar API share one httpx module, so one fake serves both."""

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            return FakeResp({"scope": f"{_EVENTS} {_FREEBUSY}", "expires_in": 3600})

        def post(self, url, json=None, headers=None):
            sent.append(headers["Authorization"])
            return FakeResp({"id": "evt-1", "htmlLink": "https://calendar.google.com/event"})

    monkeypatch.setattr("booking_agent_cli.calendar_event.httpx.Client", FakeClient)
    result = runner.invoke(app, ["flow", "calendar", "create-event"])
    assert result.exit_code == 0, result.stdout
    assert sent == [f"Bearer {_TOKEN}"]
    assert load_cached_token("cli")["access_token"] == _TOKEN
    assert _TOKEN not in result.stdout


def test_availability_filters_with_a_connector_token_and_no_oauth_client(
    runner, home, monkeypatch,
):
    """Free/busy filtering reads the cached host token; it needs no Google OAuth client."""
    import time

    from booking_agent.calendar import google as gcal
    from booking_agent_cli.runtime import resolve_calendar_service

    monkeypatch.setenv(_VAR, _TOKEN)
    _tokeninfo(monkeypatch, _FREEBUSY)
    _seed()
    assert _connector(runner, "freebusy").exit_code == 0

    service = resolve_calendar_service()
    assert isinstance(service, gcal.GoogleCalendarService)
    gcal._TOKEN_STORE.clear()
    try:
        assert service.load_cached_connection("cli", "cli", "freebusy")["connected"] is True
        assert gcal._TOKEN_STORE["cli"]["access_token"] == _TOKEN
        assert gcal._TOKEN_STORE["cli"]["expires_at"] > time.time()
    finally:
        gcal._TOKEN_STORE.clear()
