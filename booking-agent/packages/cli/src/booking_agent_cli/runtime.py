"""Backend, calendar, and platform identity for booking sessions.

Configuration comes from ``booking_agent_cli.config_loader`` (env files) and the
shipped ``defaults.env`` files read through ``booking_agent.settings``.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from booking_agent.backend import USPBackend, get_usp_backend
from booking_agent.calendar import CustomerCalendarService
from booking_agent.config import load_config
from booking_agent.platform_profile import platform_profile_url


class ProfileUrlError(RuntimeError):
    """Raised when ``USP_AGENT_PROFILE_URL`` is not a URL businesses can fetch."""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_fetchable_profile_url() -> str:
    """Return the platform identity URI, or raise ``ProfileUrlError``.

    Conformant businesses reject non-``https`` identity URIs. Plain ``http`` is
    accepted only on loopback, for local development against a local merchant.
    """
    profile_url = platform_profile_url()
    parts = urlsplit(profile_url)
    scheme = parts.scheme.lower()
    if scheme == "https" or (scheme == "http" and _is_loopback(parts.hostname or "")):
        return profile_url
    raise ProfileUrlError(
        "Businesses must be able to fetch the platform profile over https, but "
        f"USP_AGENT_PROFILE_URL is {profile_url!r}. Unset it to use the hosted "
        "profile, or point it at an https staging profile."
    )


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
