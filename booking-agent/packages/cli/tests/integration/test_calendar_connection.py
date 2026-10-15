"""Tests for unified calendar connection detection."""
import pytest

from booking_agent_cli.calendar_connection import (
    calendar_connection_status,
    host_token,
    local_oauth_configured,
    read_access_token,
    remember_route,
    remembered_route,
)

_TOKEN_ENV = "TEST_HOST_CALENDAR_TOKEN"
_SECRET_TOKEN = "ya29.secret-cached-token"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # The token cache lives under the Booking Agent home; a real token there would leak in.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path / ".booking-agent"))
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv(_TOKEN_ENV, raising=False)


def test_local_oauth_configured_false_by_default():
    assert local_oauth_configured() is False


def test_local_oauth_configured_true_when_client_id_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "operator-client")
    assert local_oauth_configured() is True


def test_read_access_token_reads_the_named_variable(monkeypatch):
    monkeypatch.setenv(_TOKEN_ENV, "env-token")
    assert read_access_token(_TOKEN_ENV) == "env-token"


def test_read_access_token_uses_the_remembered_variable(monkeypatch):
    monkeypatch.setenv(_TOKEN_ENV, "env-token")
    assert read_access_token() is None
    remember_route("connector", _TOKEN_ENV)
    assert remembered_route() == ("connector", _TOKEN_ENV)
    assert read_access_token() == "env-token"


@pytest.mark.parametrize("name", ["", "1BAD", "BAD-NAME", "A B", None])
def test_host_token_rejects_invalid_variable_names(monkeypatch, name):
    monkeypatch.setenv(_TOKEN_ENV, "env-token")
    assert host_token(name) is None


def test_host_token_ignores_a_value_from_an_env_file(tmp_path, monkeypatch):
    from booking_agent_cli import config_loader

    env_file = tmp_path / ".env"
    env_file.write_text(f"{_TOKEN_ENV}=from-file\n")
    monkeypatch.setattr(config_loader, "_applied", {_TOKEN_ENV: ("from-file", env_file)})
    monkeypatch.setenv(_TOKEN_ENV, "from-file")
    assert host_token(_TOKEN_ENV) is None


def _fake_tokeninfo(monkeypatch, scope_text):
    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"scope": scope_text}

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


def test_calendar_connection_status_uses_host_token(monkeypatch):
    monkeypatch.setenv(_TOKEN_ENV, "tok-write")
    _fake_tokeninfo(
        monkeypatch,
        "https://www.googleapis.com/auth/calendar.events "
        "https://www.googleapis.com/auth/calendar.freebusy",
    )
    status = calendar_connection_status(_TOKEN_ENV)
    assert status["connected"] is True
    assert status["supports_events"] is True
    assert status["supports_freebusy"] is True
    assert "local_oauth_configured" in status


def test_calendar_connection_status_not_connected_includes_oauth_flag():
    status = calendar_connection_status()
    assert status["connected"] is False
    assert status["local_oauth_configured"] is False


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


def test_invalid_cached_token_is_removed_and_reported(monkeypatch):
    from booking_agent_cli.calendar_event import load_cached_token, write_token_cache

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


def test_cached_token_with_refresh_token_is_kept(monkeypatch):
    import json

    from booking_agent_cli.calendar_event import _user_cache_path, load_cached_token

    with open(_user_cache_path("cli"), "w") as f:
        json.dump({"access_token": _SECRET_TOKEN, "refresh_token": "r-1"}, f)
    _tokeninfo_status(monkeypatch, 401)
    status = calendar_connection_status()
    assert status["connection_problem"]["cached_token_removed"] is False
    assert load_cached_token("cli") is not None
    _assert_no_secret(status)


def test_transient_tokeninfo_failure_keeps_cache(monkeypatch):
    from booking_agent_cli.calendar_event import load_cached_token, write_token_cache

    write_token_cache("cli", _SECRET_TOKEN)
    _tokeninfo_status(monkeypatch, 503)
    status = calendar_connection_status()
    assert status["connection_problem"]["code"] == "tokeninfo_http_error"
    assert status["connection_problem"]["cached_token_removed"] is False
    assert load_cached_token("cli") is not None


def test_invalid_env_token_reports_env_source(monkeypatch):
    monkeypatch.setenv(_TOKEN_ENV, _SECRET_TOKEN)
    _tokeninfo_status(monkeypatch, 400)
    status = calendar_connection_status(_TOKEN_ENV)
    assert status["connection_problem"]["token_source"] == "env"
    assert status["connection_problem"]["cached_token_removed"] is False
    _assert_no_secret(status)
