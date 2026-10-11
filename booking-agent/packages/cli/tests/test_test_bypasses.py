"""Tests for production bypass gating."""
from __future__ import annotations

import os

from booking_agent_cli.test_bypasses import allow_test_bypasses, test_spt_from_env


def test_spt_bypass_disabled_without_flag(monkeypatch):
    monkeypatch.delenv("BOOKING_AGENT_ALLOW_TEST_BYPASSES", raising=False)
    monkeypatch.setenv("BOOKING_AGENT_UCP_TEST_SPT", "spt_should_not_read")
    assert test_spt_from_env() is None


def test_spt_bypass_enabled_in_ci_harness(monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_ALLOW_TEST_BYPASSES", "1")
    monkeypatch.setenv("BOOKING_AGENT_UCP_TEST_SPT", "spt_ci_only")
    assert test_spt_from_env() == "spt_ci_only"
    assert allow_test_bypasses() is True
