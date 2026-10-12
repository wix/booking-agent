"""Registry search response handling across registry behaviors (USP §6.3, §9.4).

The client must give the same result whether a registry reports a rejected
request at HTTP 200 in ``messages[]`` or as a 422 Problem Details body, and
whether it names fields in snake_case (the spec) or camelCase.
"""
import httpx
import pytest

import booking_agent.rest_client as rc
from booking_agent.models.platform import ServiceSearchLocation
from booking_agent.models.wire import Coordinates
from booking_agent.observability import reset_event_sink, set_event_sink
from booking_agent.rest_client import RegistrySearchRejectedError, USPRegistryRestClient

REGISTRY_URL = "https://registry.example/registry/search_services"

FILTER_REQUIRED = (
    "At least one filter is required (location, verticals, categories, query, "
    "price_range or duration_range)."
)

SNAKE_CASE_RESPONSE = {
    "usp": {"version": "2026-08-20", "status": "success"},
    "services": [
        {
            "service_id": "svc-1",
            "service_name": "AC Repair",
            "business": {
                "id": "biz-1",
                "profile_url": "https://hvac.example/.well-known/ucp",
                "deployment_mode": "ucp_native",
                "name": "HVAC-Koby",
            },
            "category": "Our Services",
            "category_ids": ["cat-1"],
            "duration_minutes": 60,
            "pricing": {"model": "fixed", "amount": "100", "currency": "USD"},
            "timezone": "America/Phoenix",
        }
    ],
    "pagination": {"cursor": "next", "has_more": True},
    "messages": [],
}

CAMEL_CASE_RESPONSE = {
    "usp": {"version": "2026-08-20", "status": "success"},
    "services": [
        {
            "serviceId": "svc-1",
            "serviceName": "AC Repair",
            "business": {
                "id": "biz-1",
                "profileUrl": "https://hvac.example/.well-known/ucp",
                "deploymentMode": "ucp_native",
                "name": "HVAC-Koby",
            },
            "category": "Our Services",
            "categoryIds": ["cat-1"],
            "durationMinutes": 60,
            "pricing": {"model": "fixed", "amount": "100", "currency": "USD"},
            "timezone": "America/Phoenix",
            "paymentTiming": "",
        }
    ],
    "pagination": {"cursor": "next", "hasMore": True},
    "messages": [],
}


@pytest.fixture
def registry(monkeypatch):
    """Route the registry client to a handler; records every request sent."""
    state = {"handler": None, "requests": []}
    original = rc.traced_async_client

    def client_factory(**kwargs):
        def dispatch(request: httpx.Request) -> httpx.Response:
            state["requests"].append(request)
            return state["handler"](request)

        kwargs["transport"] = httpx.MockTransport(dispatch)
        return original(**kwargs)

    monkeypatch.setattr(rc, "traced_async_client", client_factory)
    return state


@pytest.fixture
def events():
    captured: list = []
    set_event_sink(captured.append)
    yield captured
    reset_event_sink()


async def _search(query="ac repair", **kwargs):
    return await USPRegistryRestClient.search_registry_for_services(
        REGISTRY_URL, query, **kwargs
    )


@pytest.mark.asyncio
async def test_validation_error_at_http_200_raises_rejection(registry, events):
    registry["handler"] = lambda _r: httpx.Response(200, json={
        "usp": {"version": "2026-08-20", "status": "error"},
        "services": [],
        "pagination": {"hasMore": False},
        "messages": [{
            "type": "error",
            "code": "validation_error",
            "content": FILTER_REQUIRED,
            "severity": "requires_buyer_input",
            "path": "",
            "contentType": "",
        }],
    })

    with pytest.raises(RegistrySearchRejectedError) as info:
        await _search()

    assert info.value.code == "validation_error"
    assert info.value.message == FILTER_REQUIRED
    assert info.value.http_status == 200
    assert [e["event"] for e in events] == ["registry_search_error"]
    assert events[0]["error_class"] == "malformed_request"


@pytest.mark.asyncio
async def test_validation_error_as_422_problem_details_raises_same_rejection(registry):
    registry["handler"] = lambda _r: httpx.Response(
        422,
        json={
            "type": "https://usp-protocol.dev/errors/validation-error",
            "title": "Unprocessable Entity",
            "status": 422,
            "detail": FILTER_REQUIRED,
        },
        headers={"Content-Type": "application/problem+json"},
    )

    with pytest.raises(RegistrySearchRejectedError) as info:
        await _search()

    assert info.value.code == "validation_error"
    assert info.value.message == FILTER_REQUIRED
    assert info.value.http_status == 422


@pytest.mark.asyncio
async def test_error_status_without_messages_still_raises_rejection(registry):
    registry["handler"] = lambda _r: httpx.Response(200, json={
        "usp": {"version": "2026-08-20", "status": "error"},
        "services": [],
    })

    with pytest.raises(RegistrySearchRejectedError) as info:
        await _search()

    assert info.value.http_status == 200


@pytest.mark.asyncio
async def test_warning_messages_do_not_reject_results(registry):
    body = dict(CAMEL_CASE_RESPONSE)
    body["messages"] = [{
        "type": "warning",
        "code": "field_ignored",
        "content": "desired_service_time_ranges is not supported",
        "path": "$.desired_service_time_ranges",
    }]
    registry["handler"] = lambda _r: httpx.Response(200, json=body)

    results, _paging = await _search()

    assert [r.service_id for r in results] == ["svc-1"]


@pytest.mark.asyncio
async def test_snake_case_and_camel_case_responses_parse_identically(registry):
    registry["handler"] = lambda _r: httpx.Response(200, json=SNAKE_CASE_RESPONSE)
    snake_results, snake_paging = await _search()
    registry["handler"] = lambda _r: httpx.Response(200, json=CAMEL_CASE_RESPONSE)
    camel_results, camel_paging = await _search()

    assert [r.model_dump() for r in snake_results] == [
        r.model_dump() for r in camel_results
    ]
    assert snake_paging.model_dump() == camel_paging.model_dump()
    hit = snake_results[0]
    assert hit.business.profile_url == "https://hvac.example/.well-known/ucp"
    assert hit.business.deployment_mode == "ucp_native"
    assert hit.category_ids == ["cat-1"]
    assert hit.duration_minutes == 60
    assert snake_paging.has_more is True


@pytest.mark.asyncio
async def test_blank_query_without_other_filter_is_rejected_before_sending(registry):
    registry["handler"] = lambda _r: httpx.Response(200, json=CAMEL_CASE_RESPONSE)

    with pytest.raises(RegistrySearchRejectedError) as info:
        await _search("   ")

    assert info.value.code == "validation_error"
    assert info.value.http_status is None
    assert registry["requests"] == []


@pytest.mark.asyncio
async def test_blank_query_with_location_sends_no_query(registry):
    registry["handler"] = lambda _r: httpx.Response(200, json=CAMEL_CASE_RESPONSE)

    await _search(
        "   ",
        location=ServiceSearchLocation(
            coordinates=Coordinates(lat=32.69, lng=-114.65), radius_km=40
        ),
    )

    import json

    sent = json.loads(registry["requests"][0].content)
    assert "query" not in sent
    assert sent["location"] == {
        "coordinates": {"lat": 32.69, "lng": -114.65},
        "radius_km": 40,
    }
