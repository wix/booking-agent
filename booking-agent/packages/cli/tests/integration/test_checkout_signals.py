"""Checkout signals on every UCP checkout write (USP Section 7.5.2)."""
from __future__ import annotations

import json
from typing import Any, Dict, List

import httpx
import pytest

import booking_agent.signals as signals
from booking_agent.signals import (
    AGENT_HOST_IP_KEY,
    BUYER_DEVICE_IP_ENV,
    BUYER_IP_KEY,
    USER_AGENT_KEY,
    SignalsConfigError,
)
from booking_agent.ucp_checkout import UcpCheckoutClient

CHECKOUT_BASE = "https://merchant.example/ucp/v1"
HOST_IP = "203.0.113.10"


def _checkout(status: str = "incomplete") -> Dict[str, Any]:
    return {
        "id": "cs_1",
        "status": status,
        "currency": "USD",
        "line_items": [],
        "totals": [{"type": "total", "amount": 0}],
        "links": [],
    }


def _capture(monkeypatch, response: Dict[str, Any]) -> List[Dict[str, Any]]:
    bodies: List[Dict[str, Any]] = []

    def _record(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    transport = httpx.MockTransport(_record)

    def fake_traced(**kwargs):
        from booking_agent.http_trace import traced_async_client as real

        return real(**{**kwargs, "transport": transport})

    monkeypatch.setattr("booking_agent.ucp_checkout.traced_async_client", fake_traced)
    return bodies


async def _write_all(client: UcpCheckoutClient) -> None:
    await client.create_checkout({"currency": "USD"})
    await client.update_checkout("cs_1", {"currency": "USD"})
    await client.complete_checkout("cs_1", {"payment": {"instruments": []}})


@pytest.mark.asyncio
async def test_every_checkout_write_sends_user_agent_and_host_ip_but_no_buyer_ip(monkeypatch):
    bodies = _capture(monkeypatch, _checkout())

    await _write_all(UcpCheckoutClient(CHECKOUT_BASE))

    assert len(bodies) == 3
    for body in bodies:
        assert body["signals"][AGENT_HOST_IP_KEY] == HOST_IP
        assert body["signals"][USER_AGENT_KEY].startswith("booking-agent-")
        assert "/" in body["signals"][USER_AGENT_KEY]
        assert BUYER_IP_KEY not in body["signals"]


@pytest.mark.asyncio
async def test_relayed_buyer_device_ip_is_sent_apart_from_host_ip(monkeypatch):
    monkeypatch.setenv(BUYER_DEVICE_IP_ENV, "2001:db8::1")
    bodies = _capture(monkeypatch, _checkout())

    await UcpCheckoutClient(CHECKOUT_BASE).create_checkout({"currency": "USD"})

    assert bodies[0]["signals"][BUYER_IP_KEY] == "2001:db8::1"
    assert bodies[0]["signals"][AGENT_HOST_IP_KEY] == HOST_IP


@pytest.mark.asyncio
async def test_invalid_relayed_buyer_ip_stops_the_request(monkeypatch):
    monkeypatch.setenv(BUYER_DEVICE_IP_ENV, "not-an-ip")
    bodies = _capture(monkeypatch, _checkout())

    with pytest.raises(SignalsConfigError):
        await UcpCheckoutClient(CHECKOUT_BASE).create_checkout({"currency": "USD"})
    assert bodies == []


@pytest.mark.asyncio
async def test_caller_cannot_assert_owned_signal_keys(monkeypatch):
    bodies = _capture(monkeypatch, _checkout())

    await UcpCheckoutClient(CHECKOUT_BASE).create_checkout(
        {
            "currency": "USD",
            "signals": {
                BUYER_IP_KEY: "198.51.100.99",
                USER_AGENT_KEY: "spoofed",
                "com.example.other": "kept",
            },
        }
    )

    sent = bodies[0]["signals"]
    assert BUYER_IP_KEY not in sent
    assert sent[USER_AGENT_KEY] != "spoofed"
    assert sent["com.example.other"] == "kept"


@pytest.mark.asyncio
async def test_failed_host_ip_lookup_omits_the_key_and_is_not_retried(monkeypatch):
    calls = 0

    async def _unreachable():
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline")

    signals.reset_agent_host_ip_cache()
    monkeypatch.setattr(signals, "_fetch_public_ip", _unreachable)
    bodies = _capture(monkeypatch, _checkout())

    await _write_all(UcpCheckoutClient(CHECKOUT_BASE))

    assert calls == 1
    for body in bodies:
        assert AGENT_HOST_IP_KEY not in body["signals"]
        assert USER_AGENT_KEY in body["signals"]
