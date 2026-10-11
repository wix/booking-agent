"""booking-agent configuration (USP env subset).

Reads environment variables (optionally loaded from a .env file by the CLI's
secrets layer) and exposes them as a small Config object.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .platform_profile import platform_profile_url
from .registry_url import DEFAULT_REGISTRY_URL


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


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_config() -> Config:
    """Build a Config from process environment (no .env loading here)."""
    redirect_port = _int("GOOGLE_CALENDAR_REDIRECT_PORT", 8098)
    return Config(
        usp_agent_profile_url=platform_profile_url(),
        usp_token_store_path=os.environ.get("USP_TOKEN_STORE_PATH"),
        usp_platform_name=os.environ.get("USP_PLATFORM_NAME"),
        usp_platform_contact=os.environ.get("USP_PLATFORM_CONTACT"),
        usp_registry_url=os.environ.get("USP_REGISTRY_URL", DEFAULT_REGISTRY_URL),
        usp_backend=os.environ.get("USP_BACKEND", "live").lower(),
        usp_auth_scoped_credential=_optional_strip(
            os.environ.get("BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL")
        ),
        usp_platform_return_url=os.environ.get("USP_PLATFORM_RETURN_URL"),
        booking_agent_usp_merchant_bearer_token=_optional_strip(
            os.environ.get("BOOKING_AGENT_USP_MERCHANT_BEARER")
        ),
        booking_agent_ucp_merchant_bearer_token=_optional_strip(
            os.environ.get("BOOKING_AGENT_UCP_MERCHANT_BEARER")
        ),
        google_calendar_client_id=os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", ""),
        google_calendar_client_secret=os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET", ""),
        google_calendar_redirect_port=redirect_port,
        google_calendar_token_cache_dir=os.environ.get(
            "GOOGLE_CALENDAR_TOKEN_CACHE_DIR", ".google-calendar-tokens"
        ),
        google_calendar_domain=os.environ.get("GOOGLE_CALENDAR_DOMAIN", ""),
        google_calendar_oauth_redirect_base_url=os.environ.get(
            "GOOGLE_CALENDAR_OAUTH_REDIRECT_BASE_URL"
        ) or f"http://localhost:{redirect_port}",
        google_calendar_ssl_certfile=os.environ.get("GOOGLE_CALENDAR_SSL_CERTFILE", ""),
        google_calendar_ssl_keyfile=os.environ.get("GOOGLE_CALENDAR_SSL_KEYFILE", ""),
    )
