"""Every outbound client must carry an explicit connect and overall deadline."""
from __future__ import annotations

import httpx
import pytest

from booking_agent.http_deadlines import coerce_timeout


def test_float_timeout_splits_connect_from_overall():
    timeout = coerce_timeout(15.0, connect=5.0)
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 5.0
    assert timeout.read == 15.0
    assert timeout.write == 15.0
    assert timeout.pool == 15.0


def test_connect_never_exceeds_overall():
    timeout = coerce_timeout(3.0, connect=5.0)
    assert timeout.connect == 3.0
    assert timeout.read == 3.0


def test_missing_timeout_gets_an_explicit_default():
    timeout = coerce_timeout(None)
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect is not None
    assert timeout.read is not None
    assert timeout.connect <= timeout.read


@pytest.mark.asyncio
async def test_traced_async_client_never_uses_httpx_default_timeout():
    from booking_agent.http_trace import traced_async_client

    async with traced_async_client() as client:
        timeout = client.timeout
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.connect is not None
        assert timeout.read is not None
