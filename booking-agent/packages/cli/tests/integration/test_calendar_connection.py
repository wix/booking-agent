"""Tests for unified calendar connection detection."""
import os

from booking_agent_cli.calendar_connection import (
    calendar_connection_status,
    local_oauth_configured,
    read_access_token,
)


def test_local_oauth_configured_false_by_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    assert local_oauth_configured() is False


def test_local_oauth_configured_true_when_client_id_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "operator-client")
    assert local_oauth_configured() is True


def test_read_access_token_prefers_env(monkeypatch):
    monkeypatch.setenv("GOOGLECALENDAR_ACCESS_TOKEN", "env-token")
    assert read_access_token() == "env-token"


def test_calendar_connection_status_uses_base44_env_token(monkeypatch):
    monkeypatch.setenv("GOOGLECALENDAR_ACCESS_TOKEN", "tok-write")

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "scope": (
                    "https://www.googleapis.com/auth/calendar.events "
                    "https://www.googleapis.com/auth/calendar.freebusy"
                ),
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            return FakeResp()

    monkeypatch.setattr("booking_agent.calendar.scope.httpx.Client", FakeClient)
    status = calendar_connection_status()
    assert status["connected"] is True
    assert status["supports_events"] is True
    assert status["supports_freebusy"] is True
    assert "local_oauth_configured" in status


def test_calendar_connection_status_not_connected_includes_oauth_flag(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    # The token cache lives under ~/.booking-agent; a real token there would make this connected.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLECALENDAR_ACCESS_TOKEN", raising=False)
    status = calendar_connection_status()
    assert status["connected"] is False
    assert status["local_oauth_configured"] is False


_SECRET_TOKEN = "ya29.secret-cached-token"


def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLECALENDAR_ACCESS_TOKEN", raising=False)


def _tokeninfo_status(monkeypatch, status_code):
    import httpx

    from booking_agent.calendar import scope

    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(status_code, json={"error": "invalid_token"})
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scope.httpx, "Client", _client)


def _assert_no_secret(status):
    text = repr(status)
    assert _SECRET_TOKEN not in text
    assert "googleapis" not in text
    assert "access_token" not in text


def test_invalid_cached_token_is_removed_and_reported(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_event import load_cached_token, write_token_cache

    _isolate(tmp_path, monkeypatch)
    write_token_cache("cli", _SECRET_TOKEN)
    _tokeninfo_status(monkeypatch, 400)
    status = calendar_connection_status()
    assert status["connected"] is False
    assert status["error"] == "invalid_token"
    assert status["connection_problem"] == {
        "code": "invalid_token",
        "token_source": "cache",
        "http_status": 400,
        "cached_token_removed": True,
    }
    assert load_cached_token("cli") is None
    _assert_no_secret(status)


def test_cached_token_with_refresh_token_is_kept(tmp_path, monkeypatch):
    import json

    from booking_agent_cli.calendar_event import _user_cache_path, load_cached_token

    _isolate(tmp_path, monkeypatch)
    with open(_user_cache_path("cli"), "w") as f:
        json.dump({"access_token": _SECRET_TOKEN, "refresh_token": "r-1"}, f)
    _tokeninfo_status(monkeypatch, 401)
    status = calendar_connection_status()
    assert status["connection_problem"]["cached_token_removed"] is False
    assert load_cached_token("cli") is not None
    _assert_no_secret(status)


def test_transient_tokeninfo_failure_keeps_cache(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_event import load_cached_token, write_token_cache

    _isolate(tmp_path, monkeypatch)
    write_token_cache("cli", _SECRET_TOKEN)
    _tokeninfo_status(monkeypatch, 503)
    status = calendar_connection_status()
    assert status["connection_problem"]["code"] == "tokeninfo_http_error"
    assert status["connection_problem"]["cached_token_removed"] is False
    assert load_cached_token("cli") is not None


def test_invalid_env_token_reports_env_source(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("GOOGLECALENDAR_ACCESS_TOKEN", _SECRET_TOKEN)
    _tokeninfo_status(monkeypatch, 400)
    status = calendar_connection_status()
    assert status["connection_problem"]["token_source"] == "env"
    assert status["connection_problem"]["cached_token_removed"] is False
    _assert_no_secret(status)
