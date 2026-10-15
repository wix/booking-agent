"""The local OAuth callback runs in a tracked child, so `calendar connect` never blocks."""
import asyncio
import socket
import time

import httpx
import pytest

from booking_agent.calendar import google as gcal
from booking_agent.calendar import oauth_listener as ol

_PKCE = {"code_verifier": "v", "code_challenge": "c", "user_key": "cli", "oauth_scope": "s", "mode": "freebusy"}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    yield
    ol.cleanup()


@pytest.fixture
def port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _get(url):
    for _ in range(100):
        try:
            return httpx.get(url, timeout=1)
        except httpx.TransportError:
            time.sleep(0.05)
    raise AssertionError(f"listener never answered {url}")


def _wait_until(predicate, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _port_free(port):
    try:
        ol._bind(port).close()
    except ol.CallbackPortBusy:
        return False
    return True


def test_start_returns_with_the_port_already_listening(port):
    started = time.monotonic()
    record = ol.start(port=port, state="st-1", pkce=_PKCE, mode="freebusy", timeout=30)
    assert time.monotonic() - started < 5

    assert ol.status()["status"] == "pending"
    with pytest.raises(ol.CallbackPortBusy):
        ol._bind(port)
    resp = _get(f"http://localhost:{port}/oauth/google/start/st-1")
    assert resp.status_code == 200 and "Connect your calendar" in resp.text
    assert ol.pid_alive(record["pid"])


def test_cleanup_stops_the_child_and_frees_the_port(port):
    ol.start(port=port, state="st-2", pkce=_PKCE, mode="freebusy", timeout=30)
    _get(f"http://localhost:{port}/oauth/google/start/st-2")

    assert ol.cleanup() is True
    assert _wait_until(lambda: _port_free(port))
    assert ol.status() == {"status": "none"}


def test_a_new_start_replaces_a_stale_child(port):
    ol.start(port=port, state="old", pkce=_PKCE, mode="freebusy", timeout=30)
    _get(f"http://localhost:{port}/oauth/google/start/old")

    ol.start(port=port, state="new", pkce=_PKCE, mode="events", timeout=30)
    assert _get(f"http://localhost:{port}/oauth/google/start/new").status_code == 200
    assert ol.status()["mode"] == "events"


def test_the_child_exits_by_itself_at_its_deadline(port):
    ol.start(port=port, state="st-3", pkce=_PKCE, mode="freebusy", timeout=1)
    _get(f"http://localhost:{port}/oauth/google/start/st-3")

    assert _wait_until(lambda: _port_free(port), seconds=8)
    assert ol.status()["status"] == "timeout"


def test_a_denied_consent_is_reported_as_failed(port):
    ol.start(port=port, state="st-4", pkce=_PKCE, mode="freebusy", timeout=30)
    resp = _get(f"http://localhost:{port}/oauth/google/callback?state=st-4&error=access_denied")
    assert resp.status_code == 400

    assert _wait_until(lambda: ol.status()["status"] == "failed")
    assert "access_denied" in ol.status()["error"]


def test_a_busy_port_raises_before_any_link_exists(port):
    with socket.socket() as holder:
        holder.bind(("0.0.0.0", port))
        holder.listen(1)
        with pytest.raises(ol.CallbackPortBusy):
            ol.start(port=port, state="st-5", pkce=_PKCE, mode="freebusy", timeout=30)
    assert ol.status() == {"status": "none"}


def test_initiate_connection_returns_the_link_without_waiting(port, monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CALENDAR_REDIRECT_PORT", str(port))
    service = gcal.GoogleCalendarService()

    started = time.monotonic()
    result = asyncio.run(service.initiate_connection("cli", "cli", "events", timeout=30))
    assert time.monotonic() - started < 5
    assert result["auth_url"].startswith(f"http://localhost:{port}/oauth/google/start/")
    assert not hasattr(service, "wait_for_auth")
    assert asyncio.run(service.poll_auth("cli"))["status"] == "pending"
    assert asyncio.run(service.cleanup_auth()) is True


def test_initiate_connection_without_a_client_raises_before_listening(port, monkeypatch):
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CALENDAR_REDIRECT_PORT", str(port))
    with pytest.raises(RuntimeError, match="GOOGLE_CALENDAR_CLIENT_ID"):
        asyncio.run(gcal.GoogleCalendarService().initiate_connection("cli", "cli", "freebusy"))
    assert ol.status() == {"status": "none"}
