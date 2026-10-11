"""Consumer identity stores (Link wallet, Google Calendar) and logout helpers.

Link tokens live in ``link-cli``; Google tokens live in hashed files under
``~/.booking-agent``.  Neither is part of per-booking session purge -- only
``flow logout`` clears them when the consumer asks.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

from booking_agent.config import load_config

from .session import Session, load_store, save_store, get_active_session, is_actionable, save_session


def google_calendar_token_cache_dir() -> Path:
    cfg = load_config()
    cache_dir = cfg.google_calendar_token_cache_dir or ".google-calendar-tokens"
    if os.path.isabs(cache_dir):
        return Path(cache_dir)
    session_dir = os.environ.get("BOOKING_AGENT_SESSION_DIR")
    base = Path(session_dir) if session_dir else Path.home() / ".booking-agent"
    return base / cache_dir


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
    return bool(os.environ.get("GOOGLECALENDAR_ACCESS_TOKEN", "").strip())


def reset_session_identity_gates(session: Session) -> None:
    session.link_auth_gate_completed = False
    session.calendar_gate_completed = False
    session.calendar_state = "unset"
    session.calendar_mode = "unset"


def clear_calendar_preference() -> None:
    store = load_store()
    store.last_calendar_state = None
    store.last_calendar_mode = None
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

    store = load_store()
    result["upcoming_sessions_kept"] = sum(1 for s in store.sessions if is_actionable(s))
    active = get_active_session(store)
    if active is not None:
        reset_session_identity_gates(active)
        save_session(active)
    return result
