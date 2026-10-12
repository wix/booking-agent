"""Unified Google Calendar connection detection (host token or saved token)."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, Optional, Tuple

from booking_agent.config import load_config
from booking_agent.calendar.scope import (
    TOKENINFO_ERROR_INVALID,
    connection_capabilities,
    inspect_access_token,
    scope_satisfies_mode,
)

from .calendar_event import _user_cache_path, load_cached_token, write_token_cache
from .runtime import resolve_calendar_service

TOKEN_ENV = "GOOGLECALENDAR_ACCESS_TOKEN"
_CALENDAR_SESSION_ID = "cli"


TOKEN_SOURCE_ENV = "env"
TOKEN_SOURCE_CACHE = "cache"


def read_access_token_with_source() -> Tuple[Optional[str], Optional[str]]:
    """Return (token, source) where source is ``env`` or ``cache``."""
    env_token = os.environ.get(TOKEN_ENV, "").strip()
    if env_token:
        return env_token, TOKEN_SOURCE_ENV
    cached = load_cached_token(_CALENDAR_SESSION_ID)
    if cached and cached.get("access_token"):
        return str(cached["access_token"]), TOKEN_SOURCE_CACHE
    return None, None


def read_access_token() -> Optional[str]:
    """Return a calendar access token from Base44 env or Booking Agent disk cache."""
    return read_access_token_with_source()[0]


def _remove_invalid_cached_token() -> bool:
    """Delete the booking-agent cache file unless it holds a refresh token that could renew it."""
    cached = load_cached_token(_CALENDAR_SESSION_ID)
    if not cached or cached.get("refresh_token"):
        return False
    try:
        os.remove(_user_cache_path(_CALENDAR_SESSION_ID))
    except OSError:
        return False
    return True


def _connection_problem(info: Dict[str, Any], source: Optional[str]) -> Dict[str, Any]:
    code = str(info.get("error") or TOKENINFO_ERROR_INVALID)
    problem: Dict[str, Any] = {"code": code, "token_source": source}
    if info.get("http_status") is not None:
        problem["http_status"] = info["http_status"]
    removed = False
    if source == TOKEN_SOURCE_CACHE and code == TOKENINFO_ERROR_INVALID:
        removed = _remove_invalid_cached_token()
    problem["cached_token_removed"] = removed
    return problem


def local_oauth_configured() -> bool:
    """True when the operator supplied a Google OAuth client id on this host."""
    return bool(load_config().google_calendar_client_id.strip())


def calendar_connection_status() -> Dict[str, Any]:
    """Report whether Google Calendar is connected and which modes are supported."""
    oauth_ready = local_oauth_configured()
    base: Dict[str, Any] = {"local_oauth_configured": oauth_ready}
    token, source = read_access_token_with_source()
    if token:
        scope, info = inspect_access_token(token)
        if scope:
            return {"connected": True, "scope": scope, **connection_capabilities(scope), **base}
        problem = _connection_problem(info, source)
        return {
            "connected": False,
            "error": problem["code"],
            "connection_problem": problem,
            "supports_freebusy": False,
            "supports_events": False,
            **base,
        }

    if oauth_ready:
        cal = resolve_calendar_service()
        connected = asyncio.run(cal.is_connected(_CALENDAR_SESSION_ID))
        if connected:
            return {
                "connected": True,
                "scope": "",
                "supports_freebusy": True,
                "supports_events": True,
                **base,
            }

    return {
        "connected": False,
        "supports_freebusy": False,
        "supports_events": False,
        **base,
    }


def bridge_token_cache_for_mode(mode: str, *, user_key: str = _CALENDAR_SESSION_ID) -> bool:
    """Copy the active access token into Booking Agent's disk cache when scopes allow."""
    token = read_access_token()
    if not token:
        return False
    scope, info = inspect_access_token(token)
    if not scope or not scope_satisfies_mode(scope, mode):
        return False
    expires_in = 3600
    exp = info.get("exp")
    if exp is not None:
        try:
            expires_in = max(int(float(exp) - time.time()), 60)
        except (TypeError, ValueError):
            pass
    elif info.get("expires_in") is not None:
        try:
            expires_in = int(info["expires_in"])
        except (TypeError, ValueError):
            pass
    write_token_cache(user_key, token, expires_in=expires_in)
    return True
