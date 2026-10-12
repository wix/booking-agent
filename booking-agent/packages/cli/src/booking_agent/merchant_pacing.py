"""Configurable delay between merchant-host HTTP calls to avoid burst rate limits."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, Tuple
from urllib.parse import urlparse

from . import settings


def merchant_request_delay_seconds() -> float:
    return settings.get_int("BOOKING_AGENT_MERCHANT_REQUEST_DELAY_MS", minimum=0) / 1000.0


def discovery_concurrency() -> int:
    return settings.get_int("BOOKING_AGENT_DISCOVERY_CONCURRENCY", minimum=1)


def merchant_host_concurrency() -> int:
    return settings.get_int("BOOKING_AGENT_MERCHANT_HOST_CONCURRENCY", minimum=1)


def merchant_host_from_url(url: str) -> str:
    return urlparse(url).netloc.lower()


@dataclass
class _HostState:
    slots: asyncio.Semaphore
    start_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_start_monotonic: float = 0.0


# Keyed by event loop as well as host: asyncio primitives belong to one loop.
_host_states: Dict[Tuple[int, str], _HostState] = {}


def _host_state(host: str) -> _HostState:
    key = (id(asyncio.get_running_loop()), host)
    state = _host_states.get(key)
    if state is None:
        state = _HostState(slots=asyncio.Semaphore(merchant_host_concurrency()))
        _host_states[key] = state
    return state


@asynccontextmanager
async def paced_merchant_host_request(host: str):
    """Space request starts to one host by the configured delay, with a per-host cap
    on requests in flight. Requests overlap; only their starts are paced."""
    state = _host_state(host)
    async with state.slots:
        async with state.start_lock:
            delay = merchant_request_delay_seconds()
            if delay > 0 and state.last_start_monotonic > 0:
                wait_until = state.last_start_monotonic + delay
                now = time.monotonic()
                if now < wait_until:
                    sleep_for = wait_until - now
                    await asyncio.sleep(sleep_for)
                    from booking_agent.step_timing import record_pacing_ms

                    record_pacing_ms(sleep_for * 1000.0)
            state.last_start_monotonic = time.monotonic()
        yield


def reset_merchant_pacing_for_tests() -> None:
    """Clear per-host pacing state (tests only)."""
    _host_states.clear()
