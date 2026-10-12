"""Live Google Calendar integration via OAuth 2.0 + FreeBusy API.

Implements the Authorization Code flow with PKCE to obtain user consent,
then queries the Google Calendar FreeBusy endpoint for busy intervals.

The local OAuth callback is served on a short-lived loopback HTTP server.
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Any

import httpx

from booking_agent.http_deadlines import coerce_timeout
from booking_agent.http_trace import traced_async_client

from ..config import load_config
from ..settings import google_calendar_token_dir
from . import CustomerCalendarService
from .scope import (
    CALENDAR_MODE_FREEBUSY,
    inspect_access_token,
    oauth_scope_param,
    scope_satisfies_mode,
)

logger = logging.getLogger(__name__)



def _client_id() -> str:
    return load_config().google_calendar_client_id


def _client_secret() -> str:
    return load_config().google_calendar_client_secret


def _redirect_base_url() -> str:
    return load_config().google_calendar_oauth_redirect_base_url or ""


def _redirect_uri() -> str:
    return f"{_redirect_base_url()}/oauth/google/callback"


def _launcher_uri() -> str:
    return f"{_redirect_base_url()}/oauth/google/start"


def _redirect_port() -> int:
    return load_config().google_calendar_redirect_port


def _ssl_files() -> tuple:
    cfg = load_config()
    if cfg.google_calendar_ssl_certfile and cfg.google_calendar_ssl_keyfile:
        return cfg.google_calendar_ssl_certfile, cfg.google_calendar_ssl_keyfile
    return None


_GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_GOOGLE_FREEBUSY_ENDPOINT = "https://www.googleapis.com/calendar/v3/freeBusy"
_GOOGLE_PRIMARY_EVENTS_ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_GOOGLE_SCOPE_DEFAULT = "https://www.googleapis.com/auth/calendar.freebusy"

# ---------------------------------------------------------------------------
# Token persistence (per-user, directory-backed + in-memory)
# ---------------------------------------------------------------------------

_TOKEN_STORE: Dict[str, Dict[str, Any]] = {}
_PKCE_STORE: Dict[str, Dict[str, str]] = {}


def _user_cache_path(user_key: str) -> str:
    """Return the per-user token cache file path (creates dir if needed)."""
    cache_dir = google_calendar_token_dir()
    os.makedirs(cache_dir, exist_ok=True)
    safe_name = hashlib.sha256(user_key.encode()).hexdigest()[:16]
    return os.path.join(cache_dir, f"{safe_name}.json")


def _load_cached_token(user_key: str) -> Optional[Dict[str, Any]]:
    """Load a user's cached token from the file cache."""
    path = _user_cache_path(user_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            cache = json.load(f)
        if time.time() < cache.get("expires_at", 0):
            return cache
        if cache.get("refresh_token"):
            refreshed = _refresh_access_token_sync(cache["refresh_token"], user_key)
            if refreshed:
                return refreshed
    except (json.JSONDecodeError, KeyError, OSError):
        logger.debug("Failed to load calendar token cache for user", exc_info=True)
    return None


def _save_token_cache(user_key: str, token_data: Dict[str, Any]) -> None:
    """Persist token data to the per-user file cache."""
    try:
        path = _user_cache_path(user_key)
        with open(path, "w") as f:
            json.dump(token_data, f, indent=2)
        os.chmod(path, 0o600)
    except OSError:
        logger.warning("Failed to save calendar token cache", exc_info=True)


def _refresh_access_token_sync(
    refresh_token: str, user_key: str
) -> Optional[Dict[str, Any]]:
    """Synchronous token refresh using httpx (used during cache load)."""
    import httpx as _httpx
    try:
        resp = _httpx.post(
            _GOOGLE_TOKEN_ENDPOINT,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=coerce_timeout(15.0),
        )
        resp.raise_for_status()
        data = resp.json()
        token_entry = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_at": time.time() + data.get("expires_in", 3600),
            "user_key": user_key,
        }
        _save_token_cache(user_key, token_entry)
        return token_entry
    except Exception:
        logger.warning("Google Calendar token refresh failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# PKCE helpers (adapted from wix_mcp.generate_pkce_pair)
# ---------------------------------------------------------------------------

def _generate_pkce_pair() -> tuple:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(32)
    code_challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    return code_verifier, code_challenge


# ---------------------------------------------------------------------------
# OAuth callback server (Starlette, daemon thread)
# ---------------------------------------------------------------------------

def _resolve_oauth_state(request) -> Optional[str]:
    """Resolve calendar session id from path, query, or sole pending PKCE entry."""
    state = request.path_params.get("state") or request.query_params.get("state")
    if state:
        return state
    if len(_PKCE_STORE) == 1:
        sole = next(iter(_PKCE_STORE))
        logger.info(
            "OAuth start without state parameter; using sole pending session %s",
            sole,
        )
        return sole
    return None


def _build_launcher_url(session_id: str) -> str:
    """Build the local launcher URL (state in path so chat links keep it)."""
    safe_id = urllib.parse.quote(session_id, safe="")
    return f"{_launcher_uri()}/{safe_id}"


def _build_google_auth_url(state: str, code_challenge: str, scope: str) -> str:
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
    }
    return f"{_GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _build_callback_app():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse
    from starlette.routing import Route

    async def oauth_start(request: Request) -> HTMLResponse:
        state = _resolve_oauth_state(request)
        if not state:
            return HTMLResponse(
                _error_html("Missing state parameter"), status_code=400
            )

        pkce_data = _PKCE_STORE.get(state)
        if not pkce_data:
            return HTMLResponse(
                _error_html("Unknown or expired session"), status_code=400
            )

        oauth_scope = pkce_data.get("oauth_scope") or _GOOGLE_SCOPE_DEFAULT
        auth_url = _build_google_auth_url(state, pkce_data["code_challenge"], oauth_scope)
        return HTMLResponse(_launcher_html(auth_url))

    async def oauth_callback(request: Request) -> HTMLResponse:
        code = request.query_params.get("code")
        state = request.query_params.get("state")  # calendar_session_id
        error = request.query_params.get("error")

        if error:
            logger.warning("Google OAuth error: %s", error)
            return HTMLResponse(_error_html(error), status_code=400)

        if not code or not state:
            return HTMLResponse(
                _error_html("Missing code or state parameter"), status_code=400
            )

        pkce_data = _PKCE_STORE.pop(state, None)
        if not pkce_data:
            return HTMLResponse(
                _error_html("Unknown or expired session"), status_code=400
            )

        code_verifier = pkce_data["code_verifier"]
        user_key = pkce_data.get("user_key", state)

        token_data = await _exchange_code_for_token(code, code_verifier)
        if not token_data:
            return HTMLResponse(
                _error_html("Token exchange failed"), status_code=500
            )

        token_entry = {
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": time.time() + token_data.get("expires_in", 3600),
            "user_key": user_key,
        }
        _TOKEN_STORE[state] = token_entry
        _save_token_cache(user_key, token_entry)
        logger.info("Google Calendar token stored for session %s", state)
        return HTMLResponse(_success_html())

    app = Starlette(
        routes=[
            Route("/oauth/google/start/{state}", oauth_start),
            Route("/oauth/google/start", oauth_start),
            Route("/oauth/google/callback", oauth_callback),
        ]
    )
    return app


_callback_server_started = False


def _start_callback_server() -> None:
    """Start the OAuth callback HTTP server in a daemon thread (idempotent)."""
    global _callback_server_started
    if _callback_server_started:
        return
    _callback_server_started = True
    ssl_files = _ssl_files()
    port = _redirect_port()

    def _run():
        import uvicorn

        ssl_kwargs = {}
        if ssl_files:
            ssl_kwargs["ssl_certfile"], ssl_kwargs["ssl_keyfile"] = ssl_files
        uvicorn.run(
            _build_callback_app(),
            host="0.0.0.0",
            port=port,
            log_level="warning",
            **ssl_kwargs,
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info(
        "Google Calendar OAuth callback server started on %s://0.0.0.0:%d",
        "https" if ssl_files else "http",
        port,
    )


# ---------------------------------------------------------------------------
# Token exchange (adapted from wix_mcp.exchange_code_for_token)
# ---------------------------------------------------------------------------

async def _exchange_code_for_token(
    code: str, code_verifier: str
) -> Optional[dict]:
    try:
        async with traced_async_client(timeout=15.0) as client:
            resp = await client.post(
                _GOOGLE_TOKEN_ENDPOINT,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _redirect_uri(),
                    "client_id": _client_id(),
                    "client_secret": _client_secret(),
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception:
        logger.warning("Google token exchange failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# HTML responses (adapted from wix_mcp.OAuthCallbackHandler)
# ---------------------------------------------------------------------------

def _close_script() -> str:
    """JS that notifies sibling tabs and closes a script-opened popup."""
    return """
try{var c=new BroadcastChannel('calendar-auth');c.postMessage({type:'oauth-complete'});c.close()}catch(e){}
setTimeout(function(){
  window.close();
  setTimeout(function(){
    var el=document.getElementById('close-hint');
    if(el) el.textContent='You can close this tab.';
  },300);
},800);
"""


def _launcher_html(auth_url: str) -> str:
    auth_url_js = json.dumps(auth_url)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Calendar</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Google Sans',system-ui,-apple-system,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#f8f9fa}}
.card{{text-align:center;padding:56px 48px 48px;max-width:440px;width:100%}}
h1{{font-size:24px;font-weight:500;color:#202124;margin-bottom:10px}}
p{{font-size:16px;color:#5f6368;line-height:1.6}}
.btn{{display:inline-block;margin-top:24px;padding:12px 36px;
  font-size:15px;font-weight:500;color:#fff;background:#1a73e8;
  border:none;border-radius:24px;cursor:pointer}}
.btn:hover{{background:#1557b0}}
.hint{{margin-top:28px;color:#80868b;font-size:14px}}
</style></head>
<body><div class="card" id="card">
  <h1>Connect your calendar</h1>
  <p id="status">Opening Google sign-in&hellip;</p>
  <button class="btn" id="open-btn" style="display:none" type="button">Open sign-in window</button>
  <p class="hint" id="close-hint"></p>
</div>
<script>
var AUTH_URL={auth_url_js};
var popup=null;
function onComplete(){{
  document.getElementById('status').textContent='Calendar connected.';
  document.getElementById('open-btn').style.display='none';
  var hint=document.getElementById('close-hint');
  hint.textContent='This window will close automatically.';
  setTimeout(function(){{window.close();setTimeout(function(){{
    if(hint) hint.textContent='You can close this tab.';
  }},300);}},800);
}}
function openPopup(){{
  popup=window.open(AUTH_URL,'google-oauth','width=520,height=680');
  if(!popup||popup.closed){{
    document.getElementById('status').textContent='Your browser blocked the sign-in popup.';
    document.getElementById('open-btn').style.display='inline-block';
    return;
  }}
  document.getElementById('open-btn').style.display='none';
}}
document.getElementById('open-btn').addEventListener('click',openPopup);
openPopup();
try{{
  var ch=new BroadcastChannel('calendar-auth');
  ch.onmessage=function(e){{if(e.data.type==='oauth-complete'){{ch.close();onComplete();}}}};
}}catch(e){{}}
</script>
</body></html>"""


def _success_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calendar Connected</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Google Sans',system-ui,-apple-system,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#f8f9fa}}
.card{{text-align:center;padding:56px 48px 48px;max-width:440px;width:100%}}
.icon{{width:88px;height:88px;margin:0 auto 28px;border-radius:50%;
  background:#e8f5e9;display:flex;align-items:center;justify-content:center}}
.icon svg{{width:44px;height:44px;stroke:#1e8e3e;stroke-width:2.5;fill:none;
  stroke-linecap:round;stroke-linejoin:round;
  stroke-dasharray:30;stroke-dashoffset:30;animation:draw .5s .2s forwards}}
@keyframes draw{{to{{stroke-dashoffset:0}}}}
h1{{font-size:28px;font-weight:500;color:#202124;margin-bottom:10px}}
p{{font-size:16px;color:#5f6368;line-height:1.6}}
.fade{{opacity:0;animation:fadeUp .4s .1s forwards}}
@keyframes fadeUp{{to{{opacity:1;transform:translateY(0)}}}}
.fade{{transform:translateY(12px)}}
</style></head>
<body><div class="card fade">
  <div class="icon"><svg viewBox="0 0 24 24"><polyline points="4 12 10 18 20 6"/></svg></div>
  <h1>Calendar connected</h1>
  <p>Your schedule will be used to filter<br>available booking times.</p>
  <p class="hint" id="close-hint" style="margin-top:28px;color:#80868b;font-size:14px">This window will close automatically.</p>
</div>
<script>{_close_script()}
</script>
</body></html>"""


def _error_html(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connection Failed</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Google Sans',system-ui,-apple-system,sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:#f8f9fa}}
.card{{text-align:center;padding:56px 48px 48px;max-width:440px;width:100%}}
.icon{{width:88px;height:88px;margin:0 auto 28px;border-radius:50%;
  background:#fce8e6;display:flex;align-items:center;justify-content:center}}
.icon svg{{width:44px;height:44px;stroke:#d93025;stroke-width:2.5;fill:none;
  stroke-linecap:round;stroke-linejoin:round}}
h1{{font-size:28px;font-weight:500;color:#202124;margin-bottom:10px}}
p{{font-size:16px;color:#5f6368;line-height:1.6}}
.err{{color:#d93025;margin-top:14px;font-size:14px;
  background:#fce8e6;padding:12px 18px;border-radius:10px;display:inline-block}}
.close-btn{{display:inline-block;margin-top:32px;padding:12px 36px;
  font-size:15px;font-weight:500;color:#fff;background:#1a73e8;
  border:none;border-radius:24px;cursor:pointer;letter-spacing:.25px;
  transition:background .15s}}
.close-btn:hover{{background:#1557b0}}
</style></head>
<body><div class="card">
  <div class="icon"><svg viewBox="0 0 24 24"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></div>
  <h1>Connection failed</h1>
  <p>Could not connect to your calendar.<br>Please return to the chat and try again.</p>
  <div class="err">{message}</div>
  <p style="margin-top:28px;color:#80868b;font-size:14px">You can close this tab.</p>
</div></body></html>"""


# ---------------------------------------------------------------------------
# GoogleCalendarService
# ---------------------------------------------------------------------------

class GoogleCalendarService(CustomerCalendarService):
    """Real Google Calendar OAuth 2.0 + FreeBusy API integration."""

    _OAUTH_TIMEOUT = 120  # seconds to wait for user to complete consent

    async def initiate_connection(
        self, session_id: str, user_key: str = "", mode: str = CALENDAR_MODE_FREEBUSY
    ) -> dict:
        if not _client_id():
            raise RuntimeError(
                "GOOGLE_CALENDAR_CLIENT_ID is not set. "
                "Set it in ~/.booking-agent/.env (see `booking-agent config init`) to use "
                "live Google Calendar integration."
            )

        user_key = user_key or session_id
        requested_scope = oauth_scope_param(mode)

        cached = _load_cached_token(user_key)
        if cached and cached.get("access_token"):
            scope, _info = inspect_access_token(cached["access_token"])
            if scope and scope_satisfies_mode(scope, mode):
                cached["user_key"] = user_key
                _TOKEN_STORE[session_id] = cached
                logger.info(
                    "Reusing cached Google Calendar token for session %s (user %s)",
                    session_id,
                    user_key[:6],
                )
                return {"connected": True, "scope": scope, "mode": mode}

        _start_callback_server()

        code_verifier, code_challenge = _generate_pkce_pair()
        _PKCE_STORE[session_id] = {
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
            "user_key": user_key,
            "oauth_scope": requested_scope,
            "mode": mode,
        }

        # Launcher opens Google OAuth in a popup so the callback page can call
        # window.close() (browsers block close on tabs the user opened directly).
        # State lives in the path (not ?state=) so agent/chat links are less
        # likely to drop it when presenting a shortened loopback URL.
        auth_url = _build_launcher_url(session_id)
        logger.info("Built Google Calendar OAuth launcher for session %s", session_id)
        return {"auth_url": auth_url, "mode": mode, "requested_scope": requested_scope}

    async def wait_for_auth(self, session_id: str, timeout: int = 120) -> dict:
        elapsed = 0.0
        while elapsed < timeout:
            if session_id in _TOKEN_STORE:
                logger.info("Google Calendar OAuth completed for session %s", session_id)
                return {"connected": True}
            await asyncio.sleep(1)
            elapsed += 1

        _PKCE_STORE.pop(session_id, None)
        logger.warning("Google Calendar OAuth timed out after %ds", timeout)
        return {"error": "OAuth timed out. Please try again."}

    async def is_connected(self, session_id: str) -> bool:
        entry = _TOKEN_STORE.get(session_id)
        if not entry:
            return False
        return time.time() < entry.get("expires_at", 0)

    async def get_calendar_timezone(self, session_id: str) -> Optional[str]:
        """Read ``timeZone`` from a one-row events.list (allowed by the calendar.events scope)."""
        access_token = self._fresh_access_token(session_id)
        async with traced_async_client(timeout=15.0) as client:
            resp = await client.get(
                _GOOGLE_PRIMARY_EVENTS_ENDPOINT,
                params={"maxResults": "1", "fields": "timeZone"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            zone = resp.json().get("timeZone")
        return zone if isinstance(zone, str) and zone else None

    def _fresh_access_token(self, session_id: str) -> str:
        entry = _TOKEN_STORE.get(session_id)
        if not entry or not entry.get("access_token"):
            raise RuntimeError(
                "No Google Calendar token for this session. "
                "The user may not have completed the OAuth flow."
            )

        if time.time() >= entry.get("expires_at", 0) and entry.get("refresh_token"):
            uk = entry.get("user_key", session_id)
            refreshed = _refresh_access_token_sync(entry["refresh_token"], uk)
            if refreshed:
                _TOKEN_STORE[session_id] = refreshed
                entry = refreshed
            else:
                raise RuntimeError("Google Calendar token expired and refresh failed.")
        return entry["access_token"]

    async def get_busy_times(
        self, session_id: str, time_min: str, time_max: str
    ) -> List[Dict[str, str]]:
        access_token = self._fresh_access_token(session_id)
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": "primary"}],
        }

        async with traced_async_client(timeout=15.0) as client:
            resp = await client.post(
                _GOOGLE_FREEBUSY_ENDPOINT,
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        calendars = data.get("calendars", {})
        primary = calendars.get("primary", {})
        busy_list = primary.get("busy", [])

        return [{"start": b["start"], "end": b["end"]} for b in busy_list]
