"""Persist and auto-apply remembered calendar gate choices across bookings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from booking_agent.calendar.scope import (
    CALENDAR_MODE_EVENTS,
    CALENDAR_MODE_FREEBUSY,
    CALENDAR_MODE_NONE,
    scope_satisfies_mode,
)

from .calendar_connection import bridge_token_cache_for_mode

from .engine import transition
from .session import Session, load_store, save_session, save_store


@dataclass
class RememberedCalendarResult:
    applied: bool
    state: str  # connected | skipped
    mode: str
    prompt: str


def save_calendar_preference(*, state: str, mode: str) -> None:
    store = load_store()
    store.last_calendar_state = state
    store.last_calendar_mode = mode
    save_store(store)


def get_calendar_preference() -> Tuple[Optional[str], Optional[str]]:
    store = load_store()
    return store.last_calendar_state, store.last_calendar_mode


def _effective_preference(
    connection: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """The choice the user saved earlier. A token found on the device is never a choice."""
    state, mode = get_calendar_preference()
    if state and mode and _pref_applicable(state, mode, connection):
        return state, mode
    return None, None


def stale_remembered_calendar_mode(connection: Dict[str, Any]) -> Optional[str]:
    """Remembered connected mode whose Google Calendar access is no longer valid."""
    if connection.get("connected"):
        return None
    state, mode = get_calendar_preference()
    if state == "connected" and mode in (CALENDAR_MODE_FREEBUSY, CALENDAR_MODE_EVENTS):
        return mode
    return None


def remembered_preference_applicable(connection: Dict[str, Any]) -> bool:
    state, mode = _effective_preference(connection)
    return _pref_applicable(state, mode, connection)


def _pref_applicable(
    state: Optional[str],
    mode: Optional[str],
    connection: Dict[str, Any],
) -> bool:
    if not state or not mode:
        return False
    if not connection.get("connected"):
        return False
    if state == "skipped" or mode == CALENDAR_MODE_NONE:
        return True
    if mode == CALENDAR_MODE_FREEBUSY:
        return bool(connection.get("supports_freebusy"))
    if mode == CALENDAR_MODE_EVENTS:
        scope = connection.get("scope", "")
        if scope:
            return scope_satisfies_mode(scope, CALENDAR_MODE_EVENTS)
        return bool(
            connection.get("supports_events") and connection.get("supports_freebusy")
        )
    return False


def try_apply_remembered_calendar_gate(
    session: Session,
    connection: Dict[str, Any],
    *,
    before_connect_apply: Optional[Callable[[str], bool]] = None,
) -> Optional[RememberedCalendarResult]:
    """Apply the calendar preference the user saved earlier, when Google Calendar is connected."""
    state, mode = _effective_preference(connection)
    if not _pref_applicable(state, mode, connection):
        return None

    if state == "skipped" or mode == CALENDAR_MODE_NONE:
        session.calendar_gate_completed = True
        session.calendar_remembered_applied = True
        session.calendar_state = "skipped"
        session.calendar_mode = CALENDAR_MODE_NONE
        transition(session, to="CALENDAR_GATE")
        save_session(session)
        return RememberedCalendarResult(
            applied=True,
            state="skipped",
            mode=CALENDAR_MODE_NONE,
            prompt="Using your saved calendar preference (skip). Ready to query availability.",
        )

    bridge = before_connect_apply or bridge_token_cache_for_mode
    if not bridge(mode):
        return None

    session.calendar_gate_completed = True
    session.calendar_remembered_applied = True
    session.calendar_state = "connected"
    session.calendar_mode = mode
    transition(session, to="CALENDAR_GATE")
    save_session(session)
    if mode == CALENDAR_MODE_EVENTS:
        prompt = (
            "Google Calendar is already connected; using your saved preference: conflict "
            "checking and post-booking events. Ready to query availability."
        )
    else:
        prompt = (
            "Google Calendar is already connected; using your saved preference: conflict "
            "checking (free/busy only). Ready to query availability."
        )
    return RememberedCalendarResult(
        applied=True,
        state="connected",
        mode=mode,
        prompt=prompt,
    )


def complete_calendar_gate_with_preference(
    session: Session,
    *,
    state: str,
    mode: str,
) -> None:
    """Complete the gate and persist the choice for future bookings."""
    session.calendar_gate_completed = True
    session.calendar_state = state
    session.calendar_mode = mode
    transition(session, to="CALENDAR_GATE")
    save_session(session)
    save_calendar_preference(state=state, mode=mode)


def skip_calendar_gate_session_only(session: Session, *, unasked: bool = False) -> None:
    """Complete the gate as skipped for this session without persisting a store preference.

    ``unasked``: the user did not choose this (no way to connect the calendar here), so the
    booked message says no event was added.
    """
    session.calendar_gate_completed = True
    session.calendar_state = "skipped"
    session.calendar_mode = CALENDAR_MODE_NONE
    session.calendar_skipped_unasked = unasked
    transition(session, to="CALENDAR_GATE")
    save_session(session)
