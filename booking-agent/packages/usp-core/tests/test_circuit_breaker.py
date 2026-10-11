"""Per-host circuit breaker: fail fast instead of hammering a sick peer."""
from __future__ import annotations

import httpx
import pytest

from booking_agent.circuit_breaker import CircuitOpenError, HostCircuitBreaker


def test_opens_after_consecutive_failures():
    from booking_agent.observability import reset_event_sink, set_event_sink

    captured: list[dict] = []
    set_event_sink(captured.append)
    breaker = HostCircuitBreaker(failure_threshold=3, cooldown_seconds=30.0)
    url = "https://sick.example.com/usp"

    breaker.record_failure(url)
    breaker.record_failure(url)
    breaker.guard(url)

    breaker.record_failure(url)
    with pytest.raises(CircuitOpenError) as exc_info:
        breaker.guard(url)
    assert exc_info.value.host == "sick.example.com"
    assert any(e.get("event") == "circuit_open" for e in captured)
    reset_event_sink()


def test_success_resets_failure_count():
    breaker = HostCircuitBreaker(failure_threshold=2, cooldown_seconds=30.0)
    url = "https://flaky.example.com/"

    breaker.record_failure(url)
    breaker.record_success(url)
    breaker.record_failure(url)
    breaker.guard(url)


def test_half_open_after_cooldown_then_reopens_on_failure(monkeypatch):
    breaker = HostCircuitBreaker(failure_threshold=1, cooldown_seconds=10.0)
    url = "https://down.example.com/"
    now = {"t": 100.0}

    monkeypatch.setattr("booking_agent.circuit_breaker.time.monotonic", lambda: now["t"])

    breaker.record_failure(url)
    with pytest.raises(CircuitOpenError):
        breaker.guard(url)

    now["t"] = 111.0
    from booking_agent.observability import reset_event_sink, set_event_sink

    captured: list[dict] = []
    set_event_sink(captured.append)
    breaker.guard(url)
    assert any(e.get("event") == "circuit_half_open" for e in captured)
    reset_event_sink()

    breaker.record_failure(url)
    with pytest.raises(CircuitOpenError):
        breaker.guard(url)


def test_hosts_are_isolated():
    breaker = HostCircuitBreaker(failure_threshold=1, cooldown_seconds=30.0)
    breaker.record_failure("https://a.example.com/")
    breaker.guard("https://b.example.com/")
    with pytest.raises(CircuitOpenError):
        breaker.guard("https://a.example.com/")


@pytest.mark.asyncio
async def test_traced_client_trips_circuit_on_repeated_5xx():
    from booking_agent.http_trace import traced_async_client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    url = "https://merchant.example.com/v1"

    async with traced_async_client(
        transport=transport,
        timeout=httpx.Timeout(1.0, connect=0.5),
        circuit=HostCircuitBreaker(failure_threshold=2, cooldown_seconds=60.0),
    ) as client:
        assert (await client.get(url)).status_code == 503
        assert (await client.get(url)).status_code == 503
        with pytest.raises(CircuitOpenError):
            await client.get(url)
