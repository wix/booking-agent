"""Tests for the in-memory mock backend used by the CLI flow tests."""
from datetime import datetime, timedelta, timezone

import pytest

from booking_agent.models.platform import ServiceSearchIntent
from support.mock import MOCK_PROFILES, MockUSPBackend


def _intent(query: str) -> ServiceSearchIntent:
    return ServiceSearchIntent(
        query=query,
        natural_language_description=query,
        intent_expiry=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    )


@pytest.mark.asyncio
async def test_search_returns_ac_repair_for_ac_query():
    backend = MockUSPBackend()
    results = await backend.search_registry(_intent("AC repair Yuma AZ"))
    assert results, "expected at least one search result"
    names = [s.name.lower() for s in results]
    assert any("ac repair" in n for n in names)


@pytest.mark.asyncio
async def test_resolve_profile_known_hostname():
    backend = MockUSPBackend()
    profile = await backend.resolve_profile(
        "https://kobym9.wixsite.com/hvac-koby/.well-known/usp"
    )
    assert profile.business.name == "Koby's HVAC Services"


@pytest.mark.asyncio
async def test_unknown_hostname_raises():
    backend = MockUSPBackend()
    with pytest.raises(RuntimeError):
        await backend.resolve_profile("https://nope.invalid/.well-known/usp")


@pytest.mark.asyncio
async def test_create_booking_with_paid_service_yields_redirect_action():
    backend = MockUSPBackend()
    profile = MOCK_PROFILES["kobym9.wixsite.com"]
    booking = await backend.create_booking(
        profile=profile,
        service_id="e4d4b7a4-0e66-4104-891b-ba5bb4a04092",  # AC Repair (paid)
        slot_id="e4d4b7a4-0e66-4104-891b-ba5bb4a04092_slot_01",
        buyer={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.org"},
    )
    assert booking.status.value == "requires_action"
    assert booking.actions and booking.actions[0].type == "payment"
    assert booking.actions[0].continue_url.startswith("http://127.0.0.1:8099/session/")
