"""Shared fixtures for CLI tests."""
import pytest
from typer.testing import CliRunner

from support.mock import MockCalendarService, MockCheckoutServer, MockUSPBackend


@pytest.fixture
def mock_checkout_server():
    server = MockCheckoutServer()
    server.start()
    yield server
    server.stop()


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
def _reset_flow_process_globals():
    """Keep hermetic flow stubs and loopback HTTP tests order-independent."""
    import booking_agent.backend.cached_live as cached_live
    import booking_agent.backend.live as live
    import booking_agent.ucp_profile as ucp_profile
    import booking_agent_cli.booking_poll as booking_poll
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.commands.lowlevel as lowlevel
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent.merchant_pacing import reset_merchant_pacing_for_tests
    from booking_agent_cli.secrets.demo import resolve_backend

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
        cached_live.BOOKING_CACHE.clear()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def _no_buyer_timezone_network(monkeypatch):
    """Buyer-zone lookup is off by default and its geocoder never leaves the host."""
    import booking_agent_cli.timezone_context as timezone_context

    def _offline_geocoder(_params):
        raise timezone_context.BuyerTimezoneError("geocoder_unavailable")

    monkeypatch.setenv(timezone_context.LOOKUP_ENV, "off")
    monkeypatch.setattr(timezone_context, "_geocode_http", _offline_geocoder)


@pytest.fixture
def isolated_session(tmp_path, monkeypatch):
    """Chdir into a tmp dir, set test env defaults, and inject the in-memory mocks."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("BOOKING_AGENT_ALLOW_TEST_BYPASSES", "1")
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", "https://booking-agent.test/agents/demo")
    monkeypatch.setenv("USP_REGISTRY_URL", "https://registry.booking-agent.test/search")
    monkeypatch.setenv("USP_PLATFORM_RETURN_URL", "https://booking-agent.test/return")
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)

    # Inject the in-memory backend/calendar. `resolve_backend` is imported into
    # several modules, so patch the bound name in each.
    import booking_agent_cli.checkout_prereq as checkout_prereq
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
    _browser_spec = __import__(
        "booking_agent_cli.browser", fromlist=["BrowserLaunchSpec"]
    ).BrowserLaunchSpec(channel="chromium", source="playwright")

    def _fake_prereq_redirect(session):
        from booking_agent_cli.payment_helpers import browser_config_from_launch
        from booking_agent_cli.session import load_store, save_store

        store = load_store()
        store.browser_config = browser_config_from_launch(_browser_spec)
        session.browser_channel = _browser_spec.channel
        save_store(store)
        return _browser_spec

    monkeypatch.setattr(checkout_prereq, "resolve_browser_for_checkout", lambda: _browser_spec)
    monkeypatch.setattr(checkout_prereq, "run_prereq_check_redirect", _fake_prereq_redirect)
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
