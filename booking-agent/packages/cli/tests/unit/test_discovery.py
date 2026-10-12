from booking_agent.discovery import USPProfileResolver


def test_parse_profile_basic():
    data = {
        "usp": {
            "version": "2026-02-09",
            "services": {
                "dev.usp-protocol.services": [
                    {"version": "1.0", "transport": "rest", "endpoint": "https://biz.example.com/usp/v1"}
                ]
            },
            "capabilities": {
                "dev.usp-protocol.services.catalog": [{"version": "1.0"}],
                "dev.usp-protocol.services.availability": [{"version": "1.0"}],
                "dev.usp-protocol.services.bookings": [{"version": "1.0"}],
            },
            "business": {"name": "Test Biz", "timezone": "UTC", "currency": "USD"},
        }
    }
    profile = USPProfileResolver._parse_profile(data)
    assert profile.business.name == "Test Biz"
    assert profile.rest_endpoint == "https://biz.example.com/usp/v1"
    assert profile.has_catalog and profile.has_availability and profile.has_bookings


def test_extract_base_url_from_agent_card():
    base = USPProfileResolver.extract_base_url_from_agent_card_url(
        "https://biz.example.com/.well-known/agent.json"
    )
    assert base == "https://biz.example.com"
