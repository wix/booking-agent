"""Opt-in live registry integration (GH-024).

Set ``BOOKING_AGENT_STAGING_REGISTRY_TEST=1`` to exercise search plus catalog fetch
against the default public registry. ``USP_REGISTRY_URL`` may override it.

Each query in ``BOOKING_AGENT_STAGING_SEARCH_QUERY`` (comma-separated) is searched
until one returns a UCP-native USP-capable hit, whose catalog fetch is asserted.
The defaults cover a general query and the HVAC test business, because the
registry can hide every non-test site. ``BOOKING_AGENT_STAGING_UCP_PROFILE_URL``
(default: the HVAC test business) is always resolved as a UCP-native profile to
validate GH-022 parsing without relying on registry metadata.
"""
from __future__ import annotations

import os

import pytest

from booking_agent.backend.live import LiveUSPBackend
from booking_agent.headless import build_search_intent, discover_services
from booking_agent.ucp_profile import (
    fetch_and_resolve_business_profile,
    first_verified_ucp_native_service,
)

_DEFAULT_QUERIES = "haircut,AC repair Yuma"
_DEFAULT_PROFILE_URL = "https://kobym9.wixsite.com/hvac-koby/.well-known/ucp"

_staging_only = pytest.mark.skipif(
    os.environ.get("BOOKING_AGENT_STAGING_REGISTRY_TEST") != "1",
    reason="Set BOOKING_AGENT_STAGING_REGISTRY_TEST=1 to run staging registry integration",
)


def _queries() -> list[str]:
    raw = os.environ.get("BOOKING_AGENT_STAGING_SEARCH_QUERY") or _DEFAULT_QUERIES
    return [q.strip() for q in raw.split(",") if q.strip()]


@pytest.mark.asyncio
@_staging_only
async def test_staging_search_then_catalog_ucp_native() -> None:
    backend = LiveUSPBackend()
    searched = {}
    for query in _queries():
        services = await discover_services(backend, build_search_intent(query=query))
        searched[query] = len(services)
        sample = first_verified_ucp_native_service(services, backend.ucp_native_by_profile_url)
        if sample is None:
            continue
        profile = await backend.resolve_profile(sample.business.profile_url)
        svc = await backend.get_service(profile, sample.id)
        assert svc.id == sample.id
        assert svc.type, "live catalog service must include Service.type"
        assert svc.pricing.model, "live catalog service must include pricing.model"
        assert svc.pricing.currency, "live catalog service must include pricing.currency"
        return
    pytest.fail(f"no query returned a verified UCP-native USP-capable hit (hits per query: {searched})")


@pytest.mark.asyncio
@_staging_only
async def test_staging_ucp_native_profile_resolves() -> None:
    url = os.environ.get("BOOKING_AGENT_STAGING_UCP_PROFILE_URL", "").strip() or _DEFAULT_PROFILE_URL
    profile, uctx = await fetch_and_resolve_business_profile(url)
    assert uctx is not None, f"{url} must be a UCP-native profile (GH-022)"
    assert profile.rest_endpoint
