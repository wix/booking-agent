import httpx
import pytest

from booking_agent.http_retry import (
    RateLimitedError,
    retry_after_seconds,
    with_http_retry,
)


def test_retry_after_seconds_numeric():
    response = httpx.Response(429, headers={"Retry-After": "30"})
    assert retry_after_seconds(response) == 30.0


@pytest.mark.asyncio
async def test_with_http_retry_recovers_from_429():
    attempts = 0

    async def request_fn() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return httpx.Response(429, request=httpx.Request("GET", "https://example.com"))
        return httpx.Response(200, request=httpx.Request("GET", "https://example.com"))

    response = await with_http_retry(
        request_fn, max_attempts=3, base_delay=0.01, max_delay=0.02,
    )
    assert response.status_code == 200
    assert attempts == 2


@pytest.mark.asyncio
async def test_with_http_retry_raises_rate_limited_after_exhaustion(monkeypatch):
    async def request_fn() -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "120"},
            request=httpx.Request("GET", "https://example.com"),
        )

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("booking_agent.http_retry.asyncio.sleep", _no_sleep)

    with pytest.raises(RateLimitedError) as exc_info:
        await with_http_retry(
            request_fn, max_attempts=2, base_delay=0.01, max_delay=0.02,
        )
    assert exc_info.value.retry_after_seconds == 120.0


@pytest.mark.asyncio
async def test_with_http_retry_does_not_retry_400():
    attempts = 0

    async def request_fn() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, request=httpx.Request("GET", "https://example.com"))

    response = await with_http_retry(
        request_fn, max_attempts=4, base_delay=0.01, max_delay=0.02,
    )
    assert response.status_code == 400
    assert attempts == 1


@pytest.mark.asyncio
async def test_with_http_retry_recovers_from_500():
    attempts = 0

    async def request_fn() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            return httpx.Response(500, request=httpx.Request("GET", "https://example.com"))
        return httpx.Response(200, request=httpx.Request("GET", "https://example.com"))

    response = await with_http_retry(
        request_fn, max_attempts=3, base_delay=0.01, max_delay=0.02,
    )
    assert response.status_code == 200
    assert attempts == 2


@pytest.mark.asyncio
async def test_with_http_retry_recovers_from_connect_timeout():
    attempts = 0

    async def request_fn() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise httpx.ConnectTimeout("connect timed out")
        return httpx.Response(200, request=httpx.Request("GET", "https://example.com"))

    response = await with_http_retry(
        request_fn, max_attempts=3, base_delay=0.01, max_delay=0.02,
    )
    assert response.status_code == 200
    assert attempts == 2


@pytest.mark.asyncio
async def test_with_http_retry_emits_event_on_status_retry():
    from booking_agent.observability import reset_event_sink, set_event_sink

    captured: list[dict] = []
    set_event_sink(captured.append)
    calls = {"n": 0}

    async def request_fn() -> httpx.Response:
        calls["n"] += 1
        req = httpx.Request("GET", "https://example.com/")
        if calls["n"] == 1:
            return httpx.Response(503, request=req)
        return httpx.Response(200, request=req)

    response = await with_http_retry(
        request_fn,
        max_attempts=2,
        base_delay=0.01,
        max_delay=0.02,
    )
    assert response.status_code == 200
    assert any(e.get("event") == "http_retry" for e in captured)
    reset_event_sink()


@pytest.mark.asyncio
async def test_with_http_retry_does_not_retry_read_timeout():
    attempts = 0

    async def request_fn() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(httpx.ReadTimeout):
        await with_http_retry(
            request_fn, max_attempts=4, base_delay=0.01, max_delay=0.02,
        )
    assert attempts == 1
