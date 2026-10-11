"""Tests for per-host merchant HTTP pacing (#165)."""
from __future__ import annotations

import asyncio
import time

import pytest

from booking_agent.merchant_pacing import (
    merchant_host_from_url,
    paced_merchant_host_request,
    reset_merchant_pacing_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_pacing(monkeypatch):
    reset_merchant_pacing_for_tests()
    monkeypatch.setenv("BOOKING_AGENT_MERCHANT_REQUEST_DELAY_MS", "50")
    yield
    reset_merchant_pacing_for_tests()


def test_merchant_host_from_url():
    assert merchant_host_from_url("https://Biz.Example.com/usp/v1") == "biz.example.com"


@pytest.mark.asyncio
async def test_first_request_to_host_does_not_sleep():
    t0 = time.monotonic()
    async with paced_merchant_host_request("host-a.example"):
        pass
    elapsed = time.monotonic() - t0
    assert elapsed < 0.03


@pytest.mark.asyncio
async def test_second_request_to_same_host_waits():
    async with paced_merchant_host_request("host-a.example"):
        pass

    t0 = time.monotonic()
    async with paced_merchant_host_request("host-a.example"):
        pass
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.04


@pytest.mark.asyncio
async def test_different_hosts_do_not_block_each_other():
    async with paced_merchant_host_request("host-a.example"):
        pass

    async def paced_b() -> None:
        async with paced_merchant_host_request("host-b.example"):
            pass

    t0 = time.monotonic()
    await asyncio.create_task(paced_b())
    elapsed = time.monotonic() - t0
    assert elapsed < 0.03
