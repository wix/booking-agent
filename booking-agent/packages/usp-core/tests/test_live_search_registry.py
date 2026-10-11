import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from booking_agent.headless import build_search_intent, parse_service_search_location
from booking_agent.backend.live import DEFAULT_REGISTRY_SEARCH_LIMIT, LiveUSPBackend
from booking_agent.discovery import USPBusinessInfo, USPBusinessProfile, USPTransportBinding
from booking_agent.http_retry import RateLimitedError
from booking_agent.models.platform import (
    DesiredServiceTimeFrom,
    RankSignals,
    ServiceSearchResult,
    ServiceSearchResultPrice,
)
from booking_agent.models.wire import (
    CancellationPolicy,
    Channel,
    ChannelType,
    Duration,
    Pricing,
    PricingModel,
    RegistryBusinessInfo,
    ReschedulingPolicy,
    ServicePolicies,
    USPService,
)


def _profile(*, endpoint: str = "https://biz.example.com/usp/v1") -> USPBusinessProfile:
    return USPBusinessProfile(
        version="1.0",
        services={
            "dev.usp-protocol.services": [
                USPTransportBinding(
                    version="1.0",
                    transport="rest",
                    endpoint=endpoint,
                )
            ]
        },
        capabilities={},
        checkout_systems=[],
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )


def _service(service_id: str, *, business_id: str = "biz-1") -> USPService:
    return USPService(
        id=service_id,
        business_id=business_id,
        name=f"Service {service_id}",
        type="appointment",
        duration=Duration(fixed="PT60M"),
        pricing=Pricing(model=PricingModel.FIXED, amount=10000, currency="USD"),
        channel=Channel(type=ChannelType.AT_BUSINESS_LOCATION),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            confirmation_mode="auto",
            requires_payment=False,
        ),
    )


def _hit(service_id: str, *, biz_id: str = "biz-1", profile_url: str | None = None) -> ServiceSearchResult:
    return ServiceSearchResult(
        service_id=service_id,
        service_name=f"Service {service_id}",
        business=RegistryBusinessInfo(
            id=biz_id,
            name="HVAC Koby",
            profile_url=profile_url or "https://hvackoby.com/.well-known/ucp",
        ),
        category="hvac",
    )


def _intent(query: str, *, with_location: bool = False):
    kwargs = {"query": query}
    if with_location:
        kwargs["location"] = parse_service_search_location("32.6927,-114.6277")
    return build_search_intent(**kwargs)


def _stub_pacing(monkeypatch) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _noop(_host: str):
        yield

    monkeypatch.setattr(
        "booking_agent.backend.live.paced_merchant_host_request",
        _noop,
    )


@pytest.mark.asyncio
async def test_search_registry_forwards_ranking_context_and_keeps_result_order(
    monkeypatch,
):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"
    hits = [_hit("svc-b"), _hit("svc-a")]
    hits[0].rank_signals = RankSignals(
        relevance=0.9,
        coverage=0,
        density=0.1,
        soonness=0,
        hint_usable=True,
    )
    search = AsyncMock(return_value=(hits, MagicMock()))
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        search,
    )
    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        AsyncMock(return_value=(_profile(), None)),
    )
    _stub_pacing(monkeypatch)
    backend.get_service = AsyncMock(side_effect=lambda _profile, service_id: _service(service_id))

    desired = [DesiredServiceTimeFrom(start="2026-09-01T12:00:00Z")]
    services = await backend.search_registry(
        build_search_intent(
            query="ac repair",
            desired_service_time_ranges=desired,
            prefer_sooner_availability_slots=False,
        )
    )

    assert [service.id for service in services] == ["svc-b", "svc-a"]
    assert [service.metadata["search_rank"] for service in services] == [0, 1]
    assert services[0].metadata["registry_search_result"]["rankSignals"] == {
        "relevance": 0.9,
        "coverage": 0.0,
        "density": 0.1,
        "soonness": 0.0,
        "hintUsable": True,
    }
    kwargs = search.await_args.kwargs
    assert kwargs["desired_service_time_ranges"] == desired
    assert kwargs["prefer_sooner_availability_slots"] is False


@pytest.mark.asyncio
async def test_parse_service_search_location():
    loc = parse_service_search_location("32.6927,-114.6277")
    assert loc.coordinates.lat == 32.6927
    assert loc.coordinates.lng == -114.6277
    assert loc.radius_km is None
    with pytest.raises(ValueError):
        parse_service_search_location("not-valid")


@pytest.mark.asyncio
async def test_search_registry_truncates_oversized_registry_response(monkeypatch):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"

    hits = [_hit(f"svc-{i}") for i in range(9)]
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=(hits, MagicMock())),
    )
    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        AsyncMock(return_value=(_profile(), None)),
    )
    _stub_pacing(monkeypatch)

    get_service_calls: list[str] = []

    async def fake_get_service(profile, service_id: str):
        get_service_calls.append(service_id)
        return _service(service_id)

    backend.get_service = fake_get_service  # type: ignore[method-assign]

    services = await backend.search_registry(build_search_intent(query="ac repair"))
    assert len(services) == DEFAULT_REGISTRY_SEARCH_LIMIT
    assert set(get_service_calls) == {f"svc-{i}" for i in range(DEFAULT_REGISTRY_SEARCH_LIMIT)}
    assert [s.metadata["search_rank"] for s in services] == list(range(DEFAULT_REGISTRY_SEARCH_LIMIT))


@pytest.mark.asyncio
async def test_search_registry_fetches_live_catalog_per_result(monkeypatch):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"

    hits = [_hit("svc-a"), _hit("svc-b"), _hit("svc-c")]
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=(hits, MagicMock())),
    )
    profile_calls = 0

    async def fake_fetch(url: str, **kwargs):
        nonlocal profile_calls
        profile_calls += 1
        return _profile(), None

    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        fake_fetch,
    )
    _stub_pacing(monkeypatch)

    get_service_calls: list[str] = []

    async def fake_get_service(profile, service_id: str):
        get_service_calls.append(service_id)
        return _service(service_id)

    backend.get_service = fake_get_service  # type: ignore[method-assign]

    services = await backend.search_registry(build_search_intent(query="ac repair"))
    assert len(services) == 3
    assert set(get_service_calls) == {"svc-a", "svc-b", "svc-c"}
    assert profile_calls == 1
    for svc in services:
        assert svc.metadata is not None
        assert svc.metadata["catalog_source"] == "live"
        assert svc.metadata["profile_resolved"] is True


@pytest.mark.asyncio
async def test_search_registry_includes_registry_snapshot_when_profile_fails(monkeypatch):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"

    hits = [
        _hit("svc-a"),
        ServiceSearchResult(
            service_id="svc-broken",
            service_name="Broken AC",
            business=RegistryBusinessInfo(
                id="biz-broken",
                name="Cool Desert Tech",
                profile_url="https://broken.example/.well-known/ucp",
            ),
            category="hvac",
            duration_minutes=90,
            price=ServiceSearchResultPrice(amount=15000, currency="USD"),
        ),
    ]
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=(hits, MagicMock())),
    )

    async def fake_fetch(url: str, **kwargs):
        if "broken.example" in url:
            raise httpx.HTTPStatusError(
                "not found",
                request=MagicMock(),
                response=MagicMock(status_code=404),
            )
        return _profile(), None

    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        fake_fetch,
    )
    _stub_pacing(monkeypatch)

    async def fake_get_service(profile, service_id: str):
        return _service(service_id)

    backend.get_service = fake_get_service  # type: ignore[method-assign]

    services = await backend.search_registry(build_search_intent(query="ac repair"))
    assert len(services) == 2
    live = next(s for s in services if s.id == "svc-a")
    registry_only = next(s for s in services if s.id == "svc-broken")
    assert live.metadata["catalog_source"] == "live"
    assert registry_only.metadata["catalog_source"] == "registry"
    assert registry_only.metadata["profile_resolved"] is False
    assert registry_only.pricing.amount == 15000
    assert registry_only.duration.fixed == "PT90M"


@pytest.mark.asyncio
async def test_search_registry_includes_registry_snapshot_when_catalog_fails(monkeypatch):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"

    hits = [_hit("svc-missing")]
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=(hits, MagicMock())),
    )
    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        AsyncMock(return_value=(_profile(), None)),
    )
    _stub_pacing(monkeypatch)

    async def fake_get_service(profile, service_id: str):
        raise httpx.HTTPStatusError(
            "not found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )

    backend.get_service = fake_get_service  # type: ignore[method-assign]

    services = await backend.search_registry(build_search_intent(query="ac repair"))
    assert len(services) == 1
    assert services[0].id == "svc-missing"
    assert services[0].metadata["catalog_source"] == "registry"
    assert services[0].metadata["profile_resolved"] is False


@pytest.mark.asyncio
async def test_search_registry_parallel_faster_than_serial_pacing(monkeypatch):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"
    monkeypatch.setenv("BOOKING_AGENT_MERCHANT_REQUEST_DELAY_MS", "60")
    monkeypatch.setenv("BOOKING_AGENT_DISCOVERY_CONCURRENCY", "8")

    hits = [
        _hit(f"svc-{i}", biz_id=f"biz-{i}", profile_url=f"https://biz{i}.example/.well-known/ucp")
        for i in range(5)
    ]
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=(hits, MagicMock())),
    )

    async def slow_fetch(url: str, **kwargs):
        await asyncio.sleep(0.01)
        host = url.split("//")[1].split("/")[0]
        return _profile(endpoint=f"https://{host}/usp/v1"), None

    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        slow_fetch,
    )

    async def slow_get_service(profile, service_id: str):
        await asyncio.sleep(0.01)
        return _service(service_id, business_id=profile.business.name)

    backend.get_service = slow_get_service  # type: ignore[method-assign]

    t0 = time.monotonic()
    services = await backend.search_registry(build_search_intent(query="haircut"))
    elapsed = time.monotonic() - t0

    assert len(services) == 5
    serial_lower_bound = 10 * 0.06
    assert elapsed < serial_lower_bound * 0.5


@pytest.mark.asyncio
async def test_search_registry_preserves_search_rank_despite_staggered_completion(monkeypatch):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"

    hits = [
        _hit("svc-slow", biz_id="biz-slow", profile_url="https://slow.example/.well-known/ucp"),
        _hit("svc-fast", biz_id="biz-fast", profile_url="https://fast.example/.well-known/ucp"),
    ]
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=(hits, MagicMock())),
    )

    async def staggered_fetch(url: str, **kwargs):
        if "slow.example" in url:
            await asyncio.sleep(0.05)
        host = url.split("//")[1].split("/")[0]
        return _profile(endpoint=f"https://{host}/usp/v1"), None

    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        staggered_fetch,
    )
    _stub_pacing(monkeypatch)

    async def fake_get_service(profile, service_id: str):
        return _service(service_id)

    backend.get_service = fake_get_service  # type: ignore[method-assign]

    services = await backend.search_registry(build_search_intent(query="ac repair"))
    assert [s.id for s in services] == ["svc-slow", "svc-fast"]
    assert [s.metadata["search_rank"] for s in services] == [0, 1]


@pytest.mark.asyncio
async def test_search_registry_skips_pacing_on_profile_file_cache_hit(monkeypatch):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"

    profile_url = "https://cached.example/.well-known/ucp"
    hits = [_hit("svc-a", profile_url=profile_url)]
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=(hits, MagicMock())),
    )
    monkeypatch.setattr(
        "booking_agent.profile_cache.read_cached_profile",
        lambda url: (_profile(), None) if url == profile_url else None,
    )

    paced_hosts: list[str] = []
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def track_pace(host: str):
        paced_hosts.append(host)
        yield

    monkeypatch.setattr("booking_agent.backend.live.paced_merchant_host_request", track_pace)

    fetch_mock = AsyncMock()
    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        fetch_mock,
    )

    async def fake_get_service(profile, service_id: str):
        return _service(service_id)

    backend.get_service = fake_get_service  # type: ignore[method-assign]

    await backend.search_registry(build_search_intent(query="ac repair"))
    fetch_mock.assert_not_called()
    assert paced_hosts == ["biz.example.com"]


@pytest.mark.asyncio
async def test_search_registry_aborts_on_rate_limit(monkeypatch):
    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.example.com"

    hits = [
        _hit("svc-a", biz_id="biz-a", profile_url="https://a.example/.well-known/ucp"),
        _hit("svc-b", biz_id="biz-b", profile_url="https://b.example/.well-known/ucp"),
    ]
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=(hits, MagicMock())),
    )
    _stub_pacing(monkeypatch)

    async def rate_limited_fetch(url: str, **kwargs):
        if "b.example" in url:
            raise RateLimitedError(
                "rate limited",
                request=MagicMock(),
                response=MagicMock(status_code=429),
                retry_after_seconds=1.0,
            )
        return _profile(endpoint="https://a.example/usp/v1"), None

    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        rate_limited_fetch,
    )

    async def fake_get_service(profile, service_id: str):
        return _service(service_id)

    backend.get_service = fake_get_service  # type: ignore[method-assign]

    with pytest.raises(RateLimitedError):
        await backend.search_registry(build_search_intent(query="ac repair"))
