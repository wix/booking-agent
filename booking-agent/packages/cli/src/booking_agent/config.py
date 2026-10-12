"""booking-agent configuration (USP env subset).

Reads settings (process environment, then the shipped defaults) and exposes them
as a small Config object.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import settings
from .platform_profile import platform_profile_url


@dataclass
class Config:
    """Resolved USP configuration."""

    usp_agent_profile_url: str = ""
    usp_platform_name: Optional[str] = None
    usp_platform_contact: Optional[str] = None
    usp_registry_url: Optional[str] = None
    # Enforcement posture for the USP Section 10.1.6 privileged-operation floor:
    # required | preferred.  None means "use the default".
    usp_auth_scoped_credential: Optional[str] = None

    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_redirect_port: int = 8098


def _optional_strip(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def load_config() -> Config:
    """Build a Config from the process environment and the shipped defaults."""
    get = settings.get
    return Config(
        usp_agent_profile_url=platform_profile_url(),
        usp_platform_name=get("USP_PLATFORM_NAME"),
        usp_platform_contact=get("USP_PLATFORM_CONTACT"),
        usp_registry_url=get("USP_REGISTRY_URL"),
        usp_auth_scoped_credential=_optional_strip(get("BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL")),
        google_calendar_client_id=get("GOOGLE_CALENDAR_CLIENT_ID") or "",
        google_calendar_client_secret=get("GOOGLE_CALENDAR_CLIENT_SECRET") or "",
        google_calendar_redirect_port=settings.get_int("GOOGLE_CALENDAR_REDIRECT_PORT"),
    )
