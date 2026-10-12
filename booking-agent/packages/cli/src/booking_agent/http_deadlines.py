"""Explicit connect and overall HTTP deadlines.

httpx treats a bare float as one value for connect, read, write, and pool.
Callers that pass ``timeout=15.0`` therefore never distinguish a hung TCP
handshake from a slow response. ``coerce_timeout`` always returns an
``httpx.Timeout`` with a dedicated connect budget that cannot exceed the
overall deadline.
"""
from __future__ import annotations

from typing import Optional, Union

import httpx

from . import settings



def connect_timeout_seconds() -> float:
    return settings.get_float("BOOKING_AGENT_HTTP_CONNECT_TIMEOUT", minimum=0.1)


def default_overall_seconds() -> float:
    return settings.get_float("BOOKING_AGENT_HTTP_TIMEOUT", minimum=0.1)


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
