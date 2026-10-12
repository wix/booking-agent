"""UCP checkout ``signals`` for USP bookings (USP Section 7.5.2).

Every checkout create, update, and complete request carries these, and no
setting turns them off:

- ``dev.ucp.user_agent``: the installed CLI's name and version.
- ``dev.usp-protocol.agent_host_ip``: the public IP of the machine this process
  runs on. On a hosted agent that is shared by many buyers, which is why it has
  its own key.
- ``dev.ucp.buyer_ip``: only when the hosting surface relays the address it
  observed on the buyer's device.
"""
from __future__ import annotations

import ipaddress
import logging
import os
from importlib import metadata
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

USER_AGENT_KEY = "dev.ucp.user_agent"
BUYER_IP_KEY = "dev.ucp.buyer_ip"
AGENT_HOST_IP_KEY = "dev.usp-protocol.agent_host_ip"

BUYER_DEVICE_IP_ENV = "BOOKING_AGENT_BUYER_DEVICE_IP"

PUBLIC_IP_URL = "https://api.ipify.org"
_PUBLIC_IP_TIMEOUT_S = 3.0

_host_ip_cache: Optional[str] = None
_host_ip_resolved = False


class SignalsConfigError(ValueError):
    """The buyer-device declaration is present but unusable."""


def user_agent() -> str:
    try:
        return f"booking-agent-cli/{metadata.version('booking-agent-cli')}"
    except metadata.PackageNotFoundError:
        from . import __version__

        return f"booking-agent-cli/{__version__}"


def _parse_ip(raw: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


async def _fetch_public_ip() -> Optional[str]:
    async with httpx.AsyncClient(timeout=_PUBLIC_IP_TIMEOUT_S) as client:
        response = await client.get(PUBLIC_IP_URL)
        response.raise_for_status()
    return _parse_ip(response.text)


async def agent_host_ip() -> Optional[str]:
    """Public IP of this machine, observed once per process.

    A failed lookup is also remembered: the booking still proceeds without the
    key rather than paying the timeout on every checkout call.
    """
    global _host_ip_cache, _host_ip_resolved
    if _host_ip_resolved:
        return _host_ip_cache
    try:
        _host_ip_cache = await _fetch_public_ip()
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("Could not observe the agent host's public IP: %s", exc)
        _host_ip_cache = None
    _host_ip_resolved = True
    return _host_ip_cache


def reset_agent_host_ip_cache() -> None:
    global _host_ip_cache, _host_ip_resolved
    _host_ip_cache = None
    _host_ip_resolved = False


def buyer_device_ip() -> Optional[str]:
    relayed = (os.environ.get(BUYER_DEVICE_IP_ENV) or "").strip()
    if not relayed:
        return None
    parsed = _parse_ip(relayed)
    if parsed is None:
        raise SignalsConfigError(
            f"{BUYER_DEVICE_IP_ENV} is not an IPv4 or IPv6 address: {relayed!r}"
        )
    return parsed


async def build_checkout_signals() -> Dict[str, str]:
    signals = {USER_AGENT_KEY: user_agent()}
    host_ip = await agent_host_ip()
    if host_ip:
        signals[AGENT_HOST_IP_KEY] = host_ip
    buyer_ip = buyer_device_ip()
    if buyer_ip:
        signals[BUYER_IP_KEY] = buyer_ip
    return signals


async def with_checkout_signals(body: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *body* whose ``signals`` carry this platform's observations.

    Caller-supplied keys this module owns are replaced, so a request body cannot
    assert a buyer IP the platform did not observe or receive from the buyer's
    device.
    """
    supplied = body.get("signals")
    merged: Dict[str, Any] = {}
    if isinstance(supplied, dict):
        merged.update(
            {
                k: v
                for k, v in supplied.items()
                if k not in (USER_AGENT_KEY, BUYER_IP_KEY, AGENT_HOST_IP_KEY)
            }
        )
    merged.update(await build_checkout_signals())
    return {**body, "signals": merged}
