"""Consumer identity stores (Link wallet, Google Calendar) and logout helpers.

Link tokens live in ``link-cli``; Google tokens live in hashed files under
``~/.booking-agent``.  Neither is part of per-booking session purge -- only
``flow logout`` clears them when the consumer asks.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List


from .session import Session, load_store, save_store, get_active_session, is_actionable, save_session


def google_calendar_token_cache_dir() -> Path:
    from booking_agent.settings import google_calendar_token_dir

    return google_calendar_token_dir()


def clear_google_calendar_token_cache() -> List[str]:
    """Delete all cached Google Calendar token files. Returns removed paths."""
    cache_dir = google_calendar_token_cache_dir()
    removed: List[str] = []
    if not cache_dir.is_dir():
        return removed
    for path in cache_dir.glob("*.json"):
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            pass
    try:
        if cache_dir.is_dir() and not any(cache_dir.iterdir()):
            cache_dir.rmdir()
    except OSError:
        pass
    # Drop in-memory cache used by the live calendar integration.
    try:
        from booking_agent.calendar import google as google_cal

        google_cal._TOKEN_STORE.clear()
    except Exception:
        pass
    return removed


def calendar_env_token_present() -> bool:
    """True when the host still supplies a token under the variable name the agent gave."""
    from .calendar_connection import host_token

    return host_token(load_store().last_calendar_token_env) is not None


def reset_session_identity_gates(session: Session) -> None:
    session.link_auth_gate_completed = False
    session.calendar_gate_completed = False
    session.calendar_state = "unset"
    session.calendar_mode = "unset"
    session.calendar_route = None
    session.calendar_token_env = None


def clear_calendar_preference() -> None:
    store = load_store()
    store.last_calendar_state = None
    store.last_calendar_mode = None
    store.last_calendar_route = None
    store.last_calendar_token_env = None
    save_store(store)


def logout_identity(*, clear_link: bool = True) -> Dict[str, Any]:
    """Drop Link and Google identity; keep booking sessions and PoP keys."""
    from .link_cli import logout as link_logout

    result: Dict[str, Any] = {
        "link_cleared": False,
        "google_cache_cleared": [],
        "calendar_preference_cleared": False,
        "calendar_env_token_still_present": calendar_env_token_present(),
        "upcoming_sessions_kept": 0,
    }

    if clear_link:
        try:
            link_logout()
            result["link_cleared"] = True
        except Exception as exc:
            result["link_error"] = str(exc)

    result["google_cache_cleared"] = clear_google_calendar_token_cache()
    clear_calendar_preference()
    result["calendar_preference_cleared"] = True
    if result["calendar_env_token_still_present"]:
        # The platform still hands out a token this device cannot revoke. Saving "skipped"
        # keeps it unused until the user opts in to Google Calendar again.
        from .calendar_preference import save_calendar_preference

        save_calendar_preference(state="skipped", mode="none")
        result["calendar_preference_saved_as_skipped"] = True

    store = load_store()
    result["upcoming_sessions_kept"] = sum(1 for s in store.sessions if is_actionable(s))
    active = get_active_session(store)
    if active is not None:
        reset_session_identity_gates(active)
        save_session(active)
    return result
