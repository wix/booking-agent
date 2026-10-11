"""Per-host circuit breaker so a failing merchant or registry is not hammered."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional, Union

import httpx

from booking_agent.observability import emit_event


class CircuitOpenError(RuntimeError):
    """Raised when the circuit for a host is open and the cooldown has not elapsed."""

    def __init__(self, host: str, retry_after: float) -> None:
        super().__init__(
            f"circuit open for {host}; retry after {retry_after:.0f}s"
        )
        self.host = host
        self.retry_after = retry_after


def _host_key(url: Union[str, httpx.URL]) -> str:
    parsed = httpx.URL(url) if isinstance(url, str) else url
    host = parsed.host or ""
    return host.lower()


@dataclass
class _Bucket:
    failures: int = 0
    opened_at: Optional[float] = None
    half_open_emitted: bool = False


class HostCircuitBreaker:
    """Counts consecutive failures per host and sheds load once the threshold trips.

    A success closes the circuit. After ``cooldown_seconds`` an open circuit
    becomes half-open: the next call is allowed through; another failure reopens
    it immediately.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        disabled: bool = False,
    ) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.disabled = disabled
        self._buckets: dict[str, _Bucket] = {}

    @classmethod
    def from_env(cls) -> "HostCircuitBreaker":
        raw_threshold = os.environ.get("BOOKING_AGENT_CIRCUIT_FAILURE_THRESHOLD", "5")
        raw_cooldown = os.environ.get("BOOKING_AGENT_CIRCUIT_COOLDOWN_SECONDS", "30")
        disabled = os.environ.get("BOOKING_AGENT_CIRCUIT_DISABLED", "").strip().lower()
        try:
            threshold = int(raw_threshold)
        except ValueError:
            threshold = 5
        try:
            cooldown = float(raw_cooldown)
        except ValueError:
            cooldown = 30.0
        return cls(
            failure_threshold=threshold,
            cooldown_seconds=cooldown,
            disabled=disabled in {"1", "true", "yes"},
        )

    def guard(self, url: Union[str, httpx.URL]) -> None:
        if self.disabled:
            return
        host = _host_key(url)
        bucket = self._buckets.get(host)
        if bucket is None or bucket.opened_at is None:
            return
        elapsed = time.monotonic() - bucket.opened_at
        if elapsed >= self.cooldown_seconds:
            if not bucket.half_open_emitted:
                bucket.half_open_emitted = True
                emit_event("circuit_half_open", host=host)
            return
        raise CircuitOpenError(host, retry_after=self.cooldown_seconds - elapsed)

    def record_failure(self, url: Union[str, httpx.URL]) -> None:
        if self.disabled:
            return
        host = _host_key(url)
        bucket = self._buckets.setdefault(host, _Bucket())
        bucket.failures += 1
        if bucket.failures >= self.failure_threshold:
            if bucket.opened_at is None:
                bucket.opened_at = time.monotonic()
                bucket.half_open_emitted = False
                emit_event("circuit_open", host=host)
            elif bucket.half_open_emitted:
                bucket.opened_at = time.monotonic()
                bucket.half_open_emitted = False
                emit_event("circuit_open", host=host)

    def record_success(self, url: Union[str, httpx.URL]) -> None:
        if self.disabled:
            return
        host = _host_key(url)
        self._buckets[host] = _Bucket()


_default: Optional[HostCircuitBreaker] = None


def default_breaker() -> HostCircuitBreaker:
    global _default
    if _default is None:
        _default = HostCircuitBreaker.from_env()
    return _default


def reset_default_breaker() -> None:
    global _default
    _default = None
