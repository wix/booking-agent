"""Booking questions activation, and the gate that keeps it dormant until enabled."""
from __future__ import annotations

import asyncio

from booking_agent import booking_questions_negotiation as bqn
from booking_agent.namespace import CAP_BOOKING_QUESTIONS
from booking_agent.platform_profile import platform_profile_doc

V = "2026-08-20"
ALL_PARENTS = [
    "dev.usp-protocol.services.catalog",
    "dev.usp-protocol.services.availability",
    "dev.usp-protocol.services.bookings",
    "dev.usp-protocol.services.paid_bookings",
]


def _usp(*caps: str, bq: bool = True) -> dict:
    block = {"capabilities": {cap: [{"version": V}] for cap in caps}}
    if bq:
        block["capabilities"][CAP_BOOKING_QUESTIONS] = [{"version": V, "extends": ALL_PARENTS}]
    return block


def test_tripwire_hosted_platform_profile_does_not_declare_booking_questions():
    """Gate 1. Track C removes this test in the pull request that enables the extension."""
    doc = platform_profile_doc()
    assert CAP_BOOKING_QUESTIONS not in (doc.get("usp") or {}).get("capabilities", {})
    assert CAP_BOOKING_QUESTIONS not in (doc.get("ucp") or {}).get("capabilities", {})


def test_standalone_inactive_with_the_real_platform_profile():
    business = _usp(*ALL_PARENTS[:3])
    assert bqn.standalone_active_surfaces(business, platform_profile_doc()) == frozenset()


def test_standalone_active_on_shared_surfaces_only():
    business = _usp(*ALL_PARENTS[:3])
    platform = {"usp": _usp("dev.usp-protocol.services.catalog", "dev.usp-protocol.services.bookings")}
    assert bqn.standalone_active_surfaces(business, platform) == {"catalog", "bookings"}


def test_standalone_inactive_on_version_mismatch():
    business = _usp(*ALL_PARENTS[:3])
    platform = {"usp": _usp(*ALL_PARENTS[:3])}
    platform["usp"]["capabilities"][CAP_BOOKING_QUESTIONS] = [{"version": "2025-01-01"}]
    assert bqn.standalone_active_surfaces(business, platform) == frozenset()


def test_activation_skips_the_profile_fetch_when_the_business_does_not_declare_it(monkeypatch):
    calls = []

    async def _load(url):
        calls.append(url)
        return {}

    monkeypatch.setattr("booking_agent.ucp_negotiation.load_platform_profile", _load)
    result = asyncio.run(bqn.booking_questions_activation(_usp(*ALL_PARENTS[:3], bq=False)))
    assert result == frozenset() and calls == []


def test_activation_is_inactive_when_the_platform_profile_fetch_fails(monkeypatch):
    async def _load(url):
        raise RuntimeError("offline")

    monkeypatch.setattr("booking_agent.ucp_negotiation.load_platform_profile", _load)
    assert asyncio.run(bqn.booking_questions_activation(_usp(*ALL_PARENTS[:3]))) == frozenset()


def test_activation_reads_the_platform_profile_when_declared(monkeypatch):
    async def _load(url):
        return {"usp": _usp(*ALL_PARENTS[:3])}

    monkeypatch.setattr("booking_agent.ucp_negotiation.load_platform_profile", _load)
    result = asyncio.run(bqn.booking_questions_activation(_usp(*ALL_PARENTS[:3])))
    assert result == {"catalog", "availability", "bookings"}
