"""Google Calendar connection detection (host token or saved token) and route bookkeeping."""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from booking_agent.config import load_config
from booking_agent.calendar.scope import (
    CALENDAR_MODE_EVENTS,
    TOKENINFO_ERROR_INVALID,
    connection_capabilities,
    inspect_access_token,
    scope_satisfies_mode,
)

from . import config_loader
from .calendar_event import _user_cache_path, load_cached_token, write_token_cache

_CALENDAR_SESSION_ID = "cli"

ROUTE_CONNECTOR = "connector"
ROUTE_LOCAL_OAUTH = "local-oauth"
# The agent's platform writes the event itself and reports the event id back; no token.
ROUTE_DELEGATED = "delegated"
CALENDAR_ROUTES = (ROUTE_CONNECTOR, ROUTE_LOCAL_OAUTH, ROUTE_DELEGATED)

TOKEN_SOURCE_ENV = "env"
TOKEN_SOURCE_CACHE = "cache"

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def valid_token_env_name(name: Optional[str]) -> bool:
    return bool(name and _ENV_NAME.match(name))


def remembered_route() -> Tuple[Optional[str], Optional[str]]:
    """The route and token variable name the agent last connected with."""
    from .session import load_store

    store = load_store()
    return store.last_calendar_route, store.last_calendar_token_env


def remember_route(
    route: str, token_env: Optional[str], session=None, *, delegated_freebusy: bool = False,
) -> None:
    """Record the route (and variable name, never the value) for later commands.

    ``delegated_freebusy`` only means anything on the delegated route: the platform
    can read Google Calendar busy times itself, even though it cannot hand the CLI
    a token. It is ignored, and stored as ``False``, for every other route.
    """
    from .session import load_store, save_session, save_store

    freebusy = delegated_freebusy if route == ROUTE_DELEGATED else False
    if session is not None:
        session.calendar_route = route
        session.calendar_token_env = token_env
        session.calendar_delegated_freebusy = freebusy
        save_session(session)
    store = load_store()
    store.last_calendar_route = route
    store.last_calendar_token_env = token_env
    store.last_calendar_delegated_freebusy = freebusy
    save_store(store)


def scope_request(mode: str) -> str:
    """Plain-language access to request from the user's calendar connection."""
    if mode == CALENDAR_MODE_EVENTS:
        return "See free/busy times and add events to Google Calendar"
    return "See only free/busy information in Google Calendar"


def host_token(token_env: Optional[str]) -> Optional[str]:
    """The host-supplied token in ``token_env``, read only from the starting process environment."""
    if not valid_token_env_name(token_env):
        return None
    value = (config_loader.process_value(token_env) or "").strip()
    return value or None


def read_access_token_with_source(
    token_env: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Return (token, source) where source is ``env`` or ``cache``."""
    if token_env is None:
        token_env = remembered_route()[1]
    env_token = host_token(token_env)
    if env_token:
        return env_token, TOKEN_SOURCE_ENV
    cached = load_cached_token(_CALENDAR_SESSION_ID)
    if cached and cached.get("access_token"):
        return str(cached["access_token"]), TOKEN_SOURCE_CACHE
    return None, None


def read_access_token(token_env: Optional[str] = None) -> Optional[str]:
    """Return a calendar access token from the host variable or the Booking Agent disk cache."""
    return read_access_token_with_source(token_env)[0]


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


def calendar_connection_status(token_env: Optional[str] = None) -> Dict[str, Any]:
    """Report whether Google Calendar is connected and which modes are supported."""
    base: Dict[str, Any] = {"local_oauth_configured": local_oauth_configured()}
    token, source = read_access_token_with_source(token_env)
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
    return {
        "connected": False,
        "supports_freebusy": False,
        "supports_events": False,
        **base,
    }


def _expires_in(info: Dict[str, Any]) -> int:
    exp = info.get("exp")
    if exp is not None:
        try:
            return max(int(float(exp) - time.time()), 60)
        except (TypeError, ValueError):
            pass
    if info.get("expires_in") is not None:
        try:
            return int(info["expires_in"])
        except (TypeError, ValueError):
            pass
    return 3600


def cache_host_token(token: str, info: Dict[str, Any], *, user_key: str = _CALENDAR_SESSION_ID) -> str:
    """Write a validated host token into Booking Agent's private token cache."""
    return write_token_cache(user_key, token, expires_in=_expires_in(info))


def refresh_host_token(session, mode: str) -> bool:
    """On the connector route, refresh the cache from this process's host token.

    Platform connectors rotate short-lived tokens, so each command re-reads the
    variable the agent named at connect time.
    """
    route, token_env = getattr(session, "calendar_route", None), getattr(session, "calendar_token_env", None)
    if not route:
        route, token_env = remembered_route()
    if route != ROUTE_CONNECTOR:
        return False
    return bridge_token_cache_for_mode(mode, token_env=token_env)


def bridge_token_cache_for_mode(
    mode: str,
    *,
    user_key: str = _CALENDAR_SESSION_ID,
    token_env: Optional[str] = None,
) -> bool:
    """Copy the active access token into Booking Agent's disk cache when scopes allow."""
    token = read_access_token(token_env)
    if not token:
        return False
    scope, info = inspect_access_token(token)
    if not scope or not scope_satisfies_mode(scope, mode):
        return False
    cache_host_token(token, info, user_key=user_key)
    return True
