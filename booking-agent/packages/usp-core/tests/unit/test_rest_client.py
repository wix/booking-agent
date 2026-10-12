import pytest

from booking_agent.discovery import (
    USPBusinessInfo,
    USPBusinessProfile,
    USPTransportBinding,
)
from booking_agent.rest_client import USPBusinessRestClient, BookingServerError, _booking_from_response


def _profile() -> USPBusinessProfile:
    return USPBusinessProfile(
        version="1.0",
        services={
            "dev.usp-protocol.services": [
                USPTransportBinding(
                    version="1.0", transport="rest", endpoint="https://biz.example.com/usp/v1"
                )
            ]
        },
        capabilities={},
        checkout_systems=["redirect"],
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )


def test_client_constructs_from_profile():
    client = USPBusinessRestClient(_profile(), agent_profile_url="http://localhost:8016/usp/profile.json")
    assert client.endpoint == "https://biz.example.com/usp/v1"


def test_headers_include_usp_agent():
    client = USPBusinessRestClient(_profile(), agent_profile_url="http://localhost:8016/usp/profile.json")
    headers = client._headers(
        method="POST",
        url="https://biz.example.com/usp/v1/bookings",
        idempotency_key="idem-1",
    )
    assert headers["Content-Type"] == "application/json"
    assert headers["USP-Agent"].startswith('profile="')
    assert headers["Idempotency-Key"] == "idem-1"


def test_ucp_native_uses_ucp_agent_header():
    """USP-Agent is the Standalone header; UCP-Native identifies with UCP-Agent."""
    client = USPBusinessRestClient(
        _profile(),
        agent_profile_url="http://localhost:8016/usp/profile.json",
        ucp_native=True,
    )
    headers = client._headers(
        method="POST", url="https://biz.example.com/usp/v1/bookings",
    )
    assert "USP-Agent" not in headers
    assert headers["UCP-Agent"].startswith('profile="')


def test_client_rejects_profile_without_rest_endpoint():
    profile = USPBusinessProfile(
        version="1.0",
        services={},
        capabilities={},
        checkout_systems=[],
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )
    with pytest.raises(ValueError):
        USPBusinessRestClient(profile)


def test_booking_from_response_parses_valid_booking():
    data = {
        "booking": {
            "id": "bkg_001",
            "service_id": "svc_001",
            "service_name": "AC Repair",
            "slot": {"id": "slot_001", "start": "2026-06-05T10:00:00Z", "end": "2026-06-05T11:00:00Z", "duration": "PT1H"},
            "buyer": {"first_name": "Ran", "last_name": "Y", "email": "ran@example.com", "phone_number": ""},
            "party_size": 1,
            "status": "confirmed",
            "confirmation_mode": "auto",
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
        }
    }
    booking = _booking_from_response(data)
    assert booking.id == "bkg_001"
    assert booking.status.value == "confirmed"


def test_booking_from_response_remaps_non_spec_status():
    data = {
        "booking": {
            "id": "bkg_002",
            "service_id": "svc_001",
            "service_name": "AC Repair",
            "slot": {"id": "slot_001", "start": "2026-06-05T10:00:00Z", "end": "2026-06-05T11:00:00Z", "duration": "PT1H"},
            "buyer": {"first_name": "Ran", "last_name": "Y", "email": "ran@example.com", "phone_number": ""},
            "party_size": 1,
            "status": "created",  # Wix non-spec value
            "confirmation_mode": "auto",
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
        }
    }
    booking = _booking_from_response(data)
    assert booking.status.value == "confirmed"  # remapped


def test_booking_from_response_treats_empty_optional_message_severity_as_absent():
    data = {
        "booking": {
            "id": "bkg_003",
            "service_id": "svc_001",
            "service_name": "AC Repair",
            "slot": {
                "id": "slot_001",
                "start": "2026-06-05T10:00:00Z",
                "end": "2026-06-05T11:00:00Z",
                "duration": "PT1H",
            },
            "buyer": {
                "first_name": "Ran",
                "last_name": "Y",
                "email": "ran@example.com",
                "phone_number": "",
            },
            "party_size": 1,
            "status": "confirmed",
            "confirmation_mode": "auto",
            "messages": [
                {
                    "type": "info",
                    "code": "booking_confirmed",
                    "content": "Booking confirmed",
                    "severity": "",
                }
            ],
            "created_at": "2026-06-01T00:00:00Z",
            "updated_at": "2026-06-01T00:00:00Z",
        }
    }

    booking = _booking_from_response(data)

    assert booking.messages is not None
    assert booking.messages[0].severity is None


def test_booking_from_response_raises_booking_server_error_on_http200_error_body():
    """HTTP 200 error body (no booking field, messages with type=error) raises BookingServerError."""
    data = {
        "usp": {"version": "2026-02-09"},
        "messages": [
            {"type": "error", "code": "not_found", "content": "Booking not found: bkg_999"}
        ],
    }
    with pytest.raises(BookingServerError) as exc_info:
        _booking_from_response(data)
    assert exc_info.value.code == "not_found"
    assert "not found" in exc_info.value.server_message.lower()


def test_booking_from_response_raises_booking_server_error_preserves_code():
    """Error code is preserved exactly on BookingServerError."""
    data = {
        "messages": [
            {"type": "error", "code": "upstream_error", "content": "Internal Wix error"}
        ],
    }
    with pytest.raises(BookingServerError) as exc_info:
        _booking_from_response(data)
    assert exc_info.value.code == "upstream_error"


def test_booking_from_response_raises_value_error_on_truly_malformed():
    """No booking field and no messages → ValueError (truly malformed, not server error)."""
    with pytest.raises(ValueError, match="missing 'booking'"):
        _booking_from_response({"something": "unexpected"})


def test_booking_server_error_str():
    err = BookingServerError("not_found", "Booking not found: bkg_123")
    assert "not_found" in str(err)
    assert "bkg_123" in str(err)


def _availability_client(response):
    client = USPBusinessRestClient(_profile(), agent_profile_url="http://localhost:8016/usp/profile.json")
    sent = []

    async def fake_post(url, body, **_kwargs):
        sent.append((url, body))
        return response

    client._post = fake_post
    return client, sent


_SLOT = {
    "id": "s1", "service_id": "svc1", "start": "2026-10-06T14:00:00-07:00",
    "end": "2026-10-06T15:00:00-07:00", "duration": "PT1H", "state": "available",
}


@pytest.mark.asyncio
async def test_query_availability_sends_date_bounds_timezone_and_returns_zones():
    client, sent = _availability_client({
        "slots": [_SLOT],
        "business_timezone": "America/Los_Angeles",
        "applied_date_bounds_timezone": "America/Los_Angeles",
        "timezone": "Legacy/Ignored",
    })
    result = await client.query_availability(
        "svc1", "2026-10-06", "2026-10-06", timezone="America/Los_Angeles",
    )
    assert sent == [("/availability/query", {
        "service_id": "svc1", "start_date": "2026-10-06", "end_date": "2026-10-06",
        "date_bounds_timezone": "America/Los_Angeles",
    })]
    assert [s.id for s in result] == ["s1"]
    assert result.business_timezone == "America/Los_Angeles"
    assert result.applied_date_bounds_timezone == "America/Los_Angeles"


@pytest.mark.asyncio
async def test_query_availability_without_zone_omits_field_and_tolerates_missing_zones():
    client, sent = _availability_client({"slots": [_SLOT]})
    result = await client.query_availability("svc1", "2026-10-06", "2026-10-12")
    body = sent[0][1]
    assert "date_bounds_timezone" not in body and "timezone" not in body
    assert len(result) == 1
    assert result.business_timezone is None
    assert result.applied_date_bounds_timezone is None
