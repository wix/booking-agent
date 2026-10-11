"""Configurable delay between merchant-host HTTP calls to avoid burst rate limits."""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict
from urllib.parse import urlparse


def merchant_request_delay_seconds() -> float:
    raw = os.environ.get("BOOKING_AGENT_MERCHANT_REQUEST_DELAY_MS", "200")
    try:
        ms = max(0, int(raw))
    except ValueError:
        ms = 200
    return ms / 1000.0


def discovery_concurrency() -> int:
    raw = os.environ.get("BOOKING_AGENT_DISCOVERY_CONCURRENCY", "8")
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def merchant_host_from_url(url: str) -> str:
    return urlparse(url).netloc.lower()


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_http_monotonic: float = 0.0


_host_states: Dict[str, _HostState] = {}
_hosts_registry_lock = asyncio.Lock()


async def _host_state(host: str) -> _HostState:
    async with _hosts_registry_lock:
        if host not in _host_states:
            _host_states[host] = _HostState()
        return _host_states[host]


@asynccontextmanager
async def paced_merchant_host_request(host: str):
    """Pace then yield; mark host HTTP time when the caller finishes the request."""
    state = await _host_state(host)
    async with state.lock:
        delay = merchant_request_delay_seconds()
        if delay > 0 and state.last_http_monotonic > 0:
            wait_until = state.last_http_monotonic + delay
            now = time.monotonic()
            if now < wait_until:
                sleep_for = wait_until - now
                await asyncio.sleep(sleep_for)
                from booking_agent.step_timing import record_pacing_ms

                record_pacing_ms(sleep_for * 1000.0)
        try:
            yield
        finally:
            state.last_http_monotonic = time.monotonic()


def reset_merchant_pacing_for_tests() -> None:
    """Clear per-host pacing state (tests only)."""
    _host_states.clear()
