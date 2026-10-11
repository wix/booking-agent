"""Secrets mode for the SKILL.md demonstration flow (`booking-agent flow --secrets-mode demo`).

Resolves platform identity, calendar, and USP backend from environment
variables (``.env``, an explicit ``--env-file``, or the baked-in
``DEMO_DEFAULTS``). The demonstration flow talks to real USP businesses via
the live registry — it uses the ``cached-live`` backend, not any mock.

This is **not** the headless E2E harness under ``packages/cli/tests/e2e/`` (pytest only).

``DEMO_DEFAULTS`` are demo-only. Production runs use ``secrets.booking_agent``, which
applies none of them.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from booking_agent.backend import USPBackend, get_usp_backend
from booking_agent.calendar import CustomerCalendarService
from booking_agent.config import load_config
from booking_agent.registry_url import DEFAULT_REGISTRY_URL

logger = logging.getLogger(__name__)

# Default demo configuration, baked in so demo mode works from any working
# directory without an --env-file or a checked-out repo. Mirrors
# ``.env.example``. Values are only applied when not already set in the
# environment (``os.environ.setdefault``), so a real env file or exported
# variable always wins. USP_AGENT_PROFILE_URL is sent as the ``USP-Agent`` /
# ``UCP-Agent`` identity URI; remote businesses fetch it on first contact (issue #114).
DEMO_DEFAULTS = {
    "USP_AGENT_PROFILE_URL": "https://profile.booking-agent.dev/platform-profile.json",
    "USP_REGISTRY_URL": DEFAULT_REGISTRY_URL,
    "USP_BACKEND": "cached-live",
    "USP_PLATFORM_NAME": "USP Agent",
    "USP_PLATFORM_CONTACT": "support@booking-agent.dev",
    "USP_PLATFORM_RETURN_URL": "https://booking-agent.dev/return",
}


def apply_demo_defaults() -> None:
    """Fill demo env vars that are not already set, so demo works from any cwd."""
    for key, value in DEMO_DEFAULTS.items():
        os.environ.setdefault(key, value)


def load_env_file(path: Optional[str]) -> None:
    """Light .env loader — no overrides of values already in the environment."""
    if not path:
        return
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"--env-file not found: {path}")
    for raw_line in p.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
