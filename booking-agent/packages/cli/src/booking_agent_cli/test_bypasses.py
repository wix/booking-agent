"""Test-only bypass gates (CI / offline demo). Never enable in production."""
from __future__ import annotations

import os


def allow_test_bypasses() -> bool:
    return os.environ.get("BOOKING_AGENT_ALLOW_TEST_BYPASSES", "").strip() == "1"


def test_spt_from_env() -> str | None:
    if not allow_test_bypasses():
        return None
    value = os.environ.get("BOOKING_AGENT_UCP_TEST_SPT", "").strip()
    return value or None
