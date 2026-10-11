"""Explicit connect and overall HTTP deadlines.

httpx treats a bare float as one value for connect, read, write, and pool.
Callers that pass ``timeout=15.0`` therefore never distinguish a hung TCP
handshake from a slow response. ``coerce_timeout`` always returns an
``httpx.Timeout`` with a dedicated connect budget that cannot exceed the
overall deadline.
"""
from __future__ import annotations

import os
from typing import Optional, Union

import httpx

DEFAULT_OVERALL_SECONDS = 15.0
DEFAULT_CONNECT_SECONDS = 5.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0.1, float(raw))
    except ValueError:
        return default


def connect_timeout_seconds() -> float:
    return _env_float("BOOKING_AGENT_HTTP_CONNECT_TIMEOUT", DEFAULT_CONNECT_SECONDS)


def default_overall_seconds() -> float:
    return _env_float("BOOKING_AGENT_HTTP_TIMEOUT", DEFAULT_OVERALL_SECONDS)


def coerce_timeout(
    value: Union[httpx.Timeout, float, int, None] = None,
    *,
    connect: Optional[float] = None,
) -> httpx.Timeout:
    """Return an ``httpx.Timeout`` with both connect and overall (read/write/pool) set.

    ``None`` uses ``BOOKING_AGENT_HTTP_TIMEOUT`` (default 15s). A float/int is the
    overall deadline. ``connect`` defaults to ``BOOKING_AGENT_HTTP_CONNECT_TIMEOUT``
    (default 5s), capped at the overall deadline.
    """
    if isinstance(value, httpx.Timeout):
        return value
    overall = default_overall_seconds() if value is None else float(value)
    connect_budget = connect_timeout_seconds() if connect is None else float(connect)
    connect_budget = min(connect_budget, overall)
    return httpx.Timeout(overall, connect=connect_budget)
