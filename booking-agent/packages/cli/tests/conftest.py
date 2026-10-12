"""Shared fixtures for CLI tests."""
import pytest
from typer.testing import CliRunner

from support.mock import MockCalendarService, MockUSPBackend, reset_paid


@pytest.fixture(autouse=True)
def _reset_paid_bookings():
    reset_paid()
    yield
    reset_paid()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_observability_context():
    from booking_agent.observability import reset, reset_event_sink
    from booking_agent.step_timing import drain_pending_timings

    drain_pending_timings()
    reset()
    reset_event_sink()
    yield
    drain_pending_timings()
    reset()
    reset_event_sink()


@pytest.fixture(autouse=True)
def _reset_loaded_config():
    """Values an env file put into os.environ must not outlive the test that loaded it."""
    from booking_agent_cli import config_loader

    yield
    config_loader.reset()


@pytest.fixture(autouse=True)
def _reset_flow_process_globals():
    """Keep hermetic flow stubs and loopback HTTP tests order-independent."""
    import booking_agent.backend.live as live
    import booking_agent.ucp_profile as ucp_profile
    import booking_agent_cli.booking_poll as booking_poll
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.commands.lowlevel as lowlevel
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent.merchant_pacing import reset_merchant_pacing_for_tests
    from booking_agent_cli.runtime import resolve_backend

    # Capture the authoritative profile resolver before isolated_session can
    # patch it. live.py imports the same function by value, so both bindings
    # must be restored explicitly.
    fetch_and_resolve_business_profile = (
        ucp_profile.fetch_and_resolve_business_profile
    )

    def reset() -> None:
        for module in (flow, lowlevel, payment_helpers, booking_poll):
            module.resolve_backend = resolve_backend
        ucp_profile.fetch_and_resolve_business_profile = (
            fetch_and_resolve_business_profile
        )
        live.fetch_and_resolve_business_profile = (
            fetch_and_resolve_business_profile
        )
        reset_merchant_pacing_for_tests()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _no_buyer_timezone_network(monkeypatch):
    """Buyer-zone lookup is off by default and its geocoder never leaves the host."""
    import booking_agent_cli.geocode as geocode
    import booking_agent_cli.timezone_context as timezone_context

    def _offline_geocoder(_params):
        raise geocode.GeocodeError("geocoder_unavailable")

    geocode.clear_cache()
    monkeypatch.setenv(timezone_context.LOOKUP_ENV, "off")
    monkeypatch.setattr(geocode, "_geocode_http", _offline_geocoder)
    yield
    geocode.clear_cache()


@pytest.fixture
def isolated_session(tmp_path, monkeypatch):
    """Chdir into a tmp dir, set test env defaults, and inject the in-memory mocks."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", "https://booking-agent.test/agents/demo")
    monkeypatch.setenv("USP_REGISTRY_URL", "https://registry.booking-agent.test/search")
    monkeypatch.setenv("USP_PLATFORM_RETURN_URL", "https://booking-agent.test/return")
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)

    # Inject the in-memory backend/calendar. `resolve_backend` is imported into
    # several modules, so patch the bound name in each.
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.commands.lowlevel as lowlevel
    import booking_agent_cli.payment_helpers as payment_helpers
    import booking_agent_cli.booking_poll as booking_poll

    mock_backend = MockUSPBackend()
    monkeypatch.setattr(flow, "resolve_backend", lambda: mock_backend)
    monkeypatch.setattr(lowlevel, "resolve_backend", lambda: mock_backend)
    monkeypatch.setattr(payment_helpers, "resolve_backend", lambda: mock_backend)
    monkeypatch.setattr(booking_poll, "resolve_backend", lambda: mock_backend)
    monkeypatch.setattr(flow, "resolve_calendar_service", lambda: MockCalendarService())
    monkeypatch.setattr(flow, "_probe_registry", lambda url: True)

    from booking_agent.ucp_profile import (
        parse_ucp_native_context_from_document,
        usp_business_profile_from_ucp_native,
    )
    from support.mock.ucp_server import minimal_ucp_profile

    ucp_context = parse_ucp_native_context_from_document(
        minimal_ucp_profile(
            checkout_base="https://mock-ucp.test/checkout",
            usp_base="https://mock-ucp.test/usp",
        )
    )

    async def _resolve_ucp_profile(_profile_url):
        return usp_business_profile_from_ucp_native(ucp_context), ucp_context

    monkeypatch.setattr(
        "booking_agent.ucp_profile.fetch_and_resolve_business_profile",
        _resolve_ucp_profile,
    )

    async def _create_mock_ucp_checkout(session, service_id, notes, delivery_address=None):
        from types import SimpleNamespace
        from booking_agent_cli.session import BookingRef

        profile = await mock_backend.resolve_profile(session.business.profile_url)
        name_parts = (session.buyer.name or "Buyer Unknown").split(" ", 1)
        booking = await mock_backend.create_booking(
            profile=profile,
            service_id=service_id,
            slot_id=session.slot_id,
            buyer={
                "first_name": name_parts[0],
                "last_name": name_parts[1] if len(name_parts) > 1 else "Unknown",
                "email": session.buyer.email,
            },
            notes=notes,
            delivery_address=delivery_address,
        )
        booking_id = booking.id
        session.ucp_checkout_session_id = "chk_mock_ucp"
        session.ucp_checkout_amount_minor = 100
        session.ucp_merchant_quote = {
            "currency": "USD",
            "total_minor_units": 100,
        }
        session.ucp_spt_network_id = "ntwrk_mock_link"
        session.ucp_protocol_version = "2026-04-08"
        session.ucp_negotiated_capabilities = {
            "dev.ucp.shopping.checkout": "2026-04-08",
            "dev.usp-protocol.services.paid_bookings": "2026-02-09",
        }
        session.ucp_capabilities_authoritative = True
        return (
            SimpleNamespace(id=booking_id, booking_url=None),
            BookingRef(id=booking_id, checkout_path="ucp"),
            "ucp",
        )

    monkeypatch.setattr(
        payment_helpers,
        "_create_booking_via_ucp_checkout",
        _create_mock_ucp_checkout,
    )
    MockCalendarService._connected_sessions.clear()

    from booking_agent_cli.link_cli import AuthStatus

    monkeypatch.setattr(
        flow,
        "get_auth_status",
        lambda: AuthStatus(authenticated=True),
    )

    from support.mock_link_cli import mock_link_cli_user_info

    mock_link_cli_user_info(
        monkeypatch, {"name": "Ada Lovelace", "email": "ada@booking-agent.test"}
    )

    yield tmp_path
