"""Tests for ``booking_webhook`` (GH-057)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from booking_agent_cli.booking_webhook import (
    start_webhook_server,
    stop_webhook_server,
    wait_for_booking_confirmed,
)


@pytest.mark.asyncio
async def test_webhook_rejects_unsigned_when_verify_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOKING_AGENT_WEBHOOK_VERIFY", "1")
    srv, url = start_webhook_server()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(url, json={"event_id": "e1", "booking_id": "b1", "order_id": "o1"})
        assert r.status_code == 401
    finally:
        stop_webhook_server(srv)
    monkeypatch.delenv("BOOKING_AGENT_WEBHOOK_VERIFY", raising=False)


@pytest.mark.asyncio
async def test_webhook_accepts_stub_signature_when_verify_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOOKING_AGENT_WEBHOOK_VERIFY", "1")
    srv, url = start_webhook_server()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                url,
                json={"event_id": "e2", "booking_id": "b2", "order_id": "o2"},
                headers={"Signature": 'sig1=("example")'},
            )
        assert r.status_code == 200
        data = r.json()
        assert data.get("ok") is True
    finally:
        stop_webhook_server(srv)
    monkeypatch.delenv("BOOKING_AGENT_WEBHOOK_VERIFY", raising=False)


@pytest.mark.asyncio
async def test_wait_for_booking_confirmed_times_out_when_no_event() -> None:
    out = await wait_for_booking_confirmed(
        "nobody", "norder", timeout_sec=0.5, poll_interval=0.05,
    )
    assert out is None


@pytest.mark.asyncio
async def test_wait_for_booking_confirmed_returns_payload_when_posted() -> None:
    srv, url = start_webhook_server()
    try:

        async def post_later() -> None:
            await asyncio.sleep(0.3)
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    url,
                    json={"event_id": "e3", "booking_id": "b3", "order_id": "o3"},
                )

        asyncio.create_task(post_later())
        received = await wait_for_booking_confirmed("b3", "o3", timeout_sec=5.0)
        assert received is not None
        assert received.get("booking_id") == "b3"
        assert received.get("order_id") == "o3"
    finally:
        stop_webhook_server(srv)
