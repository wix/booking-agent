"""Backend and calendar resolution for booking sessions.

Configuration comes from ``booking_agent_cli.config_loader`` (env files) and the
shipped ``defaults.env`` files read through ``booking_agent.settings``.
"""
from __future__ import annotations

from booking_agent.backend import USPBackend, get_usp_backend
from booking_agent.calendar import CustomerCalendarService
from booking_agent.config import load_config


def resolve_calendar_service() -> CustomerCalendarService:
    cfg = load_config()
    if cfg.google_calendar_client_id:
        from booking_agent.calendar.google import GoogleCalendarService
        return GoogleCalendarService()
    raise RuntimeError(
        "No calendar provider configured. Set GOOGLE_CALENDAR_CLIENT_ID (and "
        "related GOOGLE_CALENDAR_* vars) to connect a calendar, or run "
        "`booking-agent flow calendar skip` to proceed without conflict checking."
    )


def resolve_backend() -> USPBackend:
    return get_usp_backend()
