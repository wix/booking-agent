"""Google Calendar OAuth launcher URL and state resolution."""

from starlette.testclient import TestClient

from booking_agent.calendar import google as gcal

_PKCE_FIXTURE = {
    "code_verifier": "verifier",
    "code_challenge": "challenge",
    "user_key": "cli",
}


def test_build_launcher_url_puts_state_in_path():
    url = gcal._build_launcher_url("cli")
    assert url.endswith("/oauth/google/start/cli")
    assert "?" not in url


def test_oauth_start_accepts_state_in_path():
    gcal._PKCE_STORE.clear()
    gcal._PKCE_STORE["cli"] = dict(_PKCE_FIXTURE)
    client = TestClient(gcal._build_callback_app())
    try:
        resp = client.get("/oauth/google/start/cli")
        assert resp.status_code == 200
        assert "Connect your calendar" in resp.text
    finally:
        gcal._PKCE_STORE.clear()


def test_oauth_start_accepts_state_query_param():
    gcal._PKCE_STORE.clear()
    gcal._PKCE_STORE["cli"] = dict(_PKCE_FIXTURE)
    client = TestClient(gcal._build_callback_app())
    try:
        resp = client.get("/oauth/google/start?state=cli")
        assert resp.status_code == 200
    finally:
        gcal._PKCE_STORE.clear()


def test_oauth_start_without_state_uses_sole_pending_session():
    gcal._PKCE_STORE.clear()
    gcal._PKCE_STORE["cli"] = dict(_PKCE_FIXTURE)
    client = TestClient(gcal._build_callback_app())
    try:
        resp = client.get("/oauth/google/start")
        assert resp.status_code == 200
        assert "Connect your calendar" in resp.text
    finally:
        gcal._PKCE_STORE.clear()


def test_oauth_start_without_state_errors_when_no_pending_session():
    gcal._PKCE_STORE.clear()
    client = TestClient(gcal._build_callback_app())
    resp = client.get("/oauth/google/start")
    assert resp.status_code == 400
    assert "Missing state parameter" in resp.text


_SECRET_TOKEN = "ya29.secret-access-token"


def _patch_tokeninfo(monkeypatch, handler):
    import httpx

    from booking_agent.calendar import scope

    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scope.httpx, "Client", _client)


def _assert_redacted(info):
    text = repr(info)
    assert _SECRET_TOKEN not in text
    assert "tokeninfo" not in text.replace("tokeninfo_", "")
    assert "access_token" not in text
    assert "googleapis" not in text


def test_inspect_access_token_redacts_invalid_token(monkeypatch):
    import httpx

    from booking_agent.calendar.scope import inspect_access_token

    _patch_tokeninfo(
        monkeypatch,
        lambda request: httpx.Response(400, json={"error": "invalid_token"}),
    )
    scope, info = inspect_access_token(_SECRET_TOKEN)
    assert scope is None
    assert info == {"error": "invalid_token", "http_status": 400}
    _assert_redacted(info)


def test_inspect_access_token_classifies_other_http_errors(monkeypatch):
    import httpx

    from booking_agent.calendar.scope import inspect_access_token

    _patch_tokeninfo(monkeypatch, lambda request: httpx.Response(503))
    scope, info = inspect_access_token(_SECRET_TOKEN)
    assert scope is None
    assert info == {"error": "tokeninfo_http_error", "http_status": 503}
    _assert_redacted(info)


def test_inspect_access_token_classifies_transport_errors(monkeypatch):
    import httpx

    from booking_agent.calendar.scope import inspect_access_token

    def _boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    _patch_tokeninfo(monkeypatch, _boom)
    scope, info = inspect_access_token(_SECRET_TOKEN)
    assert scope is None
    assert info == {"error": "tokeninfo_unavailable"}
    _assert_redacted(info)


def test_inspect_access_token_classifies_non_json_body(monkeypatch):
    import httpx

    from booking_agent.calendar.scope import inspect_access_token

    _patch_tokeninfo(monkeypatch, lambda request: httpx.Response(200, text="<html>"))
    scope, info = inspect_access_token(_SECRET_TOKEN)
    assert scope is None
    assert info == {"error": "tokeninfo_unavailable"}


def test_get_calendar_timezone_reads_events_list_time_zone(monkeypatch):
    import asyncio
    import time

    import httpx

    from booking_agent import http_trace

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"timeZone": "Asia/Jerusalem"})

    real = http_trace.traced_async_client
    monkeypatch.setattr(
        gcal, "traced_async_client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    gcal._TOKEN_STORE["cli"] = {"access_token": "tok", "expires_at": time.time() + 600}
    try:
        zone = asyncio.run(gcal.GoogleCalendarService().get_calendar_timezone("cli"))
    finally:
        gcal._TOKEN_STORE.clear()
    assert zone == "Asia/Jerusalem"
    assert seen["url"].startswith(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events?"
    )
    assert "maxResults=1" in seen["url"] and "fields=timeZone" in seen["url"]
    assert seen["auth"] == "Bearer tok"
