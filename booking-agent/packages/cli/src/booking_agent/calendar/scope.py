"""Google Calendar OAuth scope helpers shared by the Booking Agent CLI and superagent-cli."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import httpx

from booking_agent.http_deadlines import coerce_timeout

SCOPE_FREEBUSY = "https://www.googleapis.com/auth/calendar.freebusy"
SCOPE_EVENTS = "https://www.googleapis.com/auth/calendar.events"
SCOPE_FULL = "https://www.googleapis.com/auth/calendar"

CALENDAR_MODE_NONE = "none"
CALENDAR_MODE_FREEBUSY = "freebusy"
CALENDAR_MODE_EVENTS = "events"

TOKENINFO_URL = "https://www.googleapis.com/oauth2/v3/tokeninfo"

TOKENINFO_ERROR_INVALID = "invalid_token"
TOKENINFO_ERROR_HTTP = "tokeninfo_http_error"
TOKENINFO_ERROR_UNAVAILABLE = "tokeninfo_unavailable"

_WRITE_SCOPE_EXACT = {SCOPE_EVENTS, SCOPE_FULL}


def oauth_scope_param(mode: str) -> str:
    """Space-delimited scope string for Google OAuth consent."""
    if mode == CALENDAR_MODE_EVENTS:
        return f"{SCOPE_FREEBUSY} {SCOPE_EVENTS}"
    return SCOPE_FREEBUSY


def scope_string(tokeninfo: Dict[str, Any]) -> str:
    return str(tokeninfo.get("scope") or "")


def supports_freebusy(scope: str) -> bool:
    if not scope:
        return False
    parts = scope.split()
    if SCOPE_FULL in parts or SCOPE_FREEBUSY in parts:
        return True
    if supports_events(scope):
        return True
    return "calendar.readonly" in scope or "calendar.freebusy" in scope


def supports_events(scope: str) -> bool:
    if not scope:
        return False
    parts = scope.split()
    if any(p in _WRITE_SCOPE_EXACT for p in parts):
        return True
    return SCOPE_FULL in parts


def scope_satisfies_mode(scope: str, mode: str) -> bool:
    if mode == CALENDAR_MODE_EVENTS:
        return supports_freebusy(scope) and supports_events(scope)
    if mode == CALENDAR_MODE_FREEBUSY:
        return supports_freebusy(scope)
    return False


def fetch_tokeninfo(access_token: str) -> Dict[str, Any]:
    with httpx.Client(timeout=coerce_timeout(15.0)) as client:
        resp = client.get(TOKENINFO_URL, params={"access_token": access_token})
        resp.raise_for_status()
        return resp.json()


def tokeninfo_error(exc: Exception) -> Dict[str, Any]:
    """Classify a tokeninfo failure without echoing the request URL or token."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = TOKENINFO_ERROR_INVALID if status in (400, 401) else TOKENINFO_ERROR_HTTP
        return {"error": code, "http_status": status}
    return {"error": TOKENINFO_ERROR_UNAVAILABLE}


def inspect_access_token(access_token: str) -> Tuple[Optional[str], Dict[str, Any]]:
    """Return (scope_string, tokeninfo) or (None, sanitized_error_dict) on failure."""
    try:
        info = fetch_tokeninfo(access_token)
    except (httpx.HTTPError, ValueError) as e:
        return None, tokeninfo_error(e)
    if not isinstance(info, dict):
        return None, {"error": TOKENINFO_ERROR_UNAVAILABLE}
    return scope_string(info), info


def connection_capabilities(scope: str) -> Dict[str, bool]:
    return {
        "supports_freebusy": supports_freebusy(scope),
        "supports_events": supports_events(scope),
    }
