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
    usp_token_store_path: Optional[str] = None
    usp_platform_name: Optional[str] = None
    usp_platform_contact: Optional[str] = None
    usp_registry_url: Optional[str] = None
    usp_backend: str = "live"  # live | cached-live
    # Enforcement posture for the USP Section 10.1.6 privileged-operation floor:
    # required | preferred | disabled.  None means "use the default".
    usp_auth_scoped_credential: Optional[str] = None
    usp_platform_return_url: Optional[str] = None
    # Optional demo or staging bearer tokens when OAuth token store is empty (GH-023).
    booking_agent_usp_merchant_bearer_token: Optional[str] = None
    booking_agent_ucp_merchant_bearer_token: Optional[str] = None

    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_redirect_port: int = 8098
    google_calendar_token_cache_dir: str = ".google-calendar-tokens"
    google_calendar_domain: str = ""
    google_calendar_oauth_redirect_base_url: Optional[str] = None
    google_calendar_ssl_certfile: str = ""
    google_calendar_ssl_keyfile: str = ""


def _optional_strip(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def load_config() -> Config:
    """Build a Config from the process environment and the shipped defaults."""
    get = settings.get
    redirect_port = settings.get_int("GOOGLE_CALENDAR_REDIRECT_PORT")
    return Config(
        usp_agent_profile_url=platform_profile_url(),
        usp_token_store_path=get("USP_TOKEN_STORE_PATH"),
        usp_platform_name=get("USP_PLATFORM_NAME"),
        usp_platform_contact=get("USP_PLATFORM_CONTACT"),
        usp_registry_url=get("USP_REGISTRY_URL"),
        usp_backend=(get("USP_BACKEND") or "live").lower(),
        usp_auth_scoped_credential=_optional_strip(get("BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL")),
        usp_platform_return_url=get("USP_PLATFORM_RETURN_URL"),
        booking_agent_usp_merchant_bearer_token=_optional_strip(
            get("BOOKING_AGENT_USP_MERCHANT_BEARER")
        ),
        booking_agent_ucp_merchant_bearer_token=_optional_strip(
            get("BOOKING_AGENT_UCP_MERCHANT_BEARER")
        ),
        google_calendar_client_id=get("GOOGLE_CALENDAR_CLIENT_ID") or "",
        google_calendar_client_secret=get("GOOGLE_CALENDAR_CLIENT_SECRET") or "",
        google_calendar_redirect_port=redirect_port,
        google_calendar_token_cache_dir=get("GOOGLE_CALENDAR_TOKEN_CACHE_DIR") or "",
        google_calendar_domain=get("GOOGLE_CALENDAR_DOMAIN") or "",
        google_calendar_oauth_redirect_base_url=get("GOOGLE_CALENDAR_OAUTH_REDIRECT_BASE_URL")
        or f"http://localhost:{redirect_port}",
        google_calendar_ssl_certfile=get("GOOGLE_CALENDAR_SSL_CERTFILE") or "",
        google_calendar_ssl_keyfile=get("GOOGLE_CALENDAR_SSL_KEYFILE") or "",
    )
