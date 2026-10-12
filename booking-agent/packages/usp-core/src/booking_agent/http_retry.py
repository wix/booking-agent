"""HTTP retry helpers with backoff for merchant, registry, and OAuth calls."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, Optional, Set, TypeVar

import httpx

from . import settings
from booking_agent.observability import emit_event

logger = logging.getLogger(__name__)

# Retry overload and gateway failures. Do not retry 501/505 (the method or
# version will not start working) or 4xx other than 429.
RETRYABLE_STATUS_CODES: Set[int] = {429, 500, 502, 503, 504}

# Connect failures never reached the peer, so they are safe to retry even on
# POST. ReadTimeout is excluded: the server may already have processed the body.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.PoolTimeout,
)

T = TypeVar("T")


class RateLimitedError(httpx.HTTPStatusError):
    """Raised when HTTP 429 persists after all retry attempts."""

    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.retry_after_seconds = retry_after_seconds


def _max_attempts() -> int:
    return settings.get_int("BOOKING_AGENT_HTTP_MAX_RETRIES", minimum=1)


def _base_delay() -> float:
    return settings.get_float("BOOKING_AGENT_HTTP_BACKOFF_BASE", minimum=0.1)


def _max_delay() -> float:
    return settings.get_float("BOOKING_AGENT_HTTP_BACKOFF_MAX", minimum=1.0)


def retry_after_seconds(response: httpx.Response) -> Optional[float]:
    """Parse ``Retry-After`` as seconds delay, or None if absent/unparseable."""
    raw = response.headers.get("Retry-After")
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _jittered_delay(base: float) -> float:
    return base * (0.5 + random.random())


async def with_http_retry(
    request_fn: Callable[[], Awaitable[httpx.Response]],
    *,
    max_attempts: Optional[int] = None,
    base_delay: Optional[float] = None,
    max_delay: Optional[float] = None,
) -> httpx.Response:
    """Execute *request_fn* with retries on 429/5xx and connect failures.

    *request_fn* must be a zero-argument async callable returning an
    :class:`httpx.Response` (without raising on non-2xx status). Connect
    timeouts and connection errors are retried; read timeouts are not.
    """
    attempts = max_attempts if max_attempts is not None else _max_attempts()
    delay_base = base_delay if base_delay is not None else _base_delay()
    delay_cap = max_delay if max_delay is not None else _max_delay()

    delay = delay_base
    last_response: Optional[httpx.Response] = None

    for attempt in range(attempts):
        try:
            response = await request_fn()
        except RETRYABLE_EXCEPTIONS as exc:
            last_response = None
            if attempt >= attempts - 1:
                raise
            wait = _jittered_delay(min(delay, delay_cap))
            logger.warning(
                "%s on outbound HTTP - retry %d/%d in %.1fs",
                type(exc).__name__,
                attempt + 1,
                attempts,
                wait,
            )
            emit_event(
                "http_retry",
                attempt=attempt + 1,
                max_attempts=attempts,
                reason=type(exc).__name__,
            )
            await asyncio.sleep(wait)
            delay = min(delay * 2, delay_cap)
            continue

        last_response = response
        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response
        if attempt >= attempts - 1:
            break
        retry_after = retry_after_seconds(response)
        wait = retry_after if retry_after is not None else min(delay, delay_cap)
        wait = _jittered_delay(wait)
        logger.warning(
            "HTTP %s from %s - retry %d/%d in %.1fs",
            response.status_code,
            response.request.url,
            attempt + 1,
            attempts,
            wait,
        )
        emit_event(
            "http_retry",
            attempt=attempt + 1,
            max_attempts=attempts,
            status=response.status_code,
        )
        await asyncio.sleep(wait)
        delay = min(delay * 2, delay_cap)

    assert last_response is not None
    if last_response.status_code == 429:
        raise RateLimitedError(
            f"Rate limited by {last_response.request.url}",
            request=last_response.request,
            response=last_response,
            retry_after_seconds=retry_after_seconds(last_response),
        )
    last_response.raise_for_status()
    return last_response


def is_rate_limited(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitedError):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return True
    return False


def retry_after_from_exception(exc: BaseException) -> Optional[float]:
    if isinstance(exc, RateLimitedError):
        return exc.retry_after_seconds
    if isinstance(exc, httpx.HTTPStatusError):
        return retry_after_seconds(exc.response)
    return None
