"""Acceptance tests from PLAN §11.1.

These exercise the gate enforcement and session transitions. The in-memory
MockUSPBackend (injected by conftest) keeps everything hermetic — no network.
"""
import json

import pytest

from booking_agent_cli.main import app
from support.mock_link_cli import mock_link_cli_user_info


def _invoke(runner, *args):
    return runner.invoke(app, list(args))


def _emit(result):
    """Parse the agent-facing JSON object the CLI prints to stdout."""
    if not result.stdout:
        return {}
    return json.loads(result.stdout.strip().splitlines()[-1] if "\n" not in result.stdout.strip()
                      else result.stdout)


def _stdout_json(result, *, index: int = -1):
    # Commands may emit multiple JSON objects (e.g. pay request --then-complete).
    # user_message URLs are written as a bare first stdout line before JSON.
    dec = json.JSONDecoder()
    s = result.stdout.strip()
    if not s:
        return {}
    objs = []
    idx = 0
    while idx < len(s):
        while idx < len(s) and s[idx] not in "{[":
            nl = s.find("\n", idx)
            if nl == -1:
                return objs[index] if objs else {}
            idx = nl + 1
        if idx >= len(s):
            break
        obj, end = dec.raw_decode(s, idx)
        objs.append(obj)
        idx = end
        while idx < len(s) and s[idx].isspace():
            idx += 1
    return objs[index]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_REQUEST = "Book AC repair in Yuma AZ"
_PROFILE_URL = "https://kobym9.wixsite.com/hvac-koby/.well-known/usp"
_SERVICE_ID = "e4d4b7a4-0e66-4104-891b-ba5bb4a04092"


def _init(runner, *, user_request=_USER_REQUEST, force=False):
    args = ["flow", "init", "--user-request", user_request]
    if force:
        args.append("--force")
    return _invoke(runner, *args)


def _complete_checkout_mode_setup(runner) -> dict:
    """Run business use and the checkout-mode orchestrator from next_command."""
    r = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        _PROFILE_URL,
        "--service-id",
        _SERVICE_ID,
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    next_cmd = body.get("next_command") or ""
    assert next_cmd == "booking-agent flow use-ucp-checkout-mode"
    assert body["data"]["supported_checkout_modes"] == ["ucp_native"]
    r2 = _invoke(runner, "flow", "use-ucp-checkout-mode")
    assert r2.exit_code == 0, r2.stdout + r2.stderr
    return body


def _satisfy_link_auth_gate() -> None:
    """Mark Link auth gate complete for tests that skip link-auth connect."""
    from booking_agent_cli.session import load_store, save_store

    store = load_store()
    store.sessions[0].link_auth_gate_completed = True
    save_store(store)


# ---------------------------------------------------------------------------
# Basic init / status
# ---------------------------------------------------------------------------

def test_init_creates_session(runner, isolated_session):
    r = _init(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["phase"] == "INIT"
    assert (isolated_session / ".usp-session.json").exists()


def test_init_uses_hosted_profile_by_default(runner, isolated_session, monkeypatch):
    from booking_agent.platform_profile import DEFAULT_PLATFORM_PROFILE_URL

    monkeypatch.delenv("USP_AGENT_PROFILE_URL", raising=False)

    r = _init(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["data"]["agent_profile_url"] == DEFAULT_PLATFORM_PROFILE_URL


def test_init_honors_https_profile_override(runner, isolated_session, monkeypatch):
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", "https://staging.example/platform-profile.json")

    r = _init(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["agent_profile_url"] == "https://staging.example/platform-profile.json"


def test_init_rejects_plain_http_profile_url(runner, isolated_session, monkeypatch):
    """Businesses reject a non-https identity URI, so init refuses it up front."""
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", "http://profile.example/platform-profile.json")

    r = _init(runner)
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "platform_profile_url_invalid"


@pytest.mark.parametrize("host", ["127.0.0.1:8097", "localhost:8097", "[::1]:8097"])
def test_init_accepts_loopback_http_profile_url(runner, isolated_session, monkeypatch, host):
    """Local development serves the profile from a loopback merchant over http."""
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", f"http://{host}/platform-profile.json")

    r = _init(runner)
    assert r.exit_code == 0, r.stdout + r.stderr


def _user_env(text: str) -> None:
    from booking_agent_cli import config_loader

    config_loader.user_env_path().write_text(text)


def test_init_reads_the_user_env(runner, isolated_session, monkeypatch):
    from booking_agent import settings
    from booking_agent.registry_url import DEFAULT_REGISTRY_URL

    for key in ("USP_PLATFORM_NAME", "USP_REGISTRY_URL"):
        monkeypatch.delenv(key, raising=False)
    _user_env("USP_PLATFORM_NAME=From the user env\n")

    r = _init(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert settings.get("USP_PLATFORM_NAME") == "From the user env"
    assert settings.get("USP_REGISTRY_URL") == DEFAULT_REGISTRY_URL


def test_second_init_starts_at_prereq_ok(runner, isolated_session):
    r1 = _init(runner, user_request="first booking")
    assert r1.exit_code == 0
    assert _stdout_json(r1)["phase"] == "INIT"

    # Complete the first session so it's no longer active-incomplete
    _invoke(runner, "flow", "reset")

    r2 = _init(runner, user_request="second booking")
    assert r2.exit_code == 0
    body = _stdout_json(r2)
    assert body["phase"] == "PREREQ_OK", (
        "Second session should skip prereq check since one was already run"
    )


def test_init_blocks_on_active_incomplete_session(runner, isolated_session):
    _init(runner)
    r = _init(runner, user_request="another request")
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "session_active"


def test_init_force_replaces_active_incomplete_session(runner, isolated_session):
    _init(runner)
    r = _init(runner, user_request="fresh booking", force=True)
    assert r.exit_code == 0
    body = _stdout_json(r)
    assert body["phase"] in ("INIT", "PREREQ_OK")


def test_init_does_not_block_after_confirmed_session(runner, isolated_session):
    """A CONFIRMED active session should not require --force."""
    from booking_agent_cli.session import load_store, save_store, get_active_session
    _init(runner)
    # Manually advance to CONFIRMED
    store = load_store()
    session = get_active_session(store)
    session.flow_phase = "CONFIRMED"
    for i, s in enumerate(store.sessions):
        if s.id == session.id:
            store.sessions[i] = session
    save_store(store)

    r = _init(runner, user_request="new booking after confirmed")
    assert r.exit_code == 0


# ---------------------------------------------------------------------------
# Gate enforcement
# ---------------------------------------------------------------------------

def test_business_use_then_availability_without_calendar_fails(
    runner, isolated_session, monkeypatch
):
    _init(runner)
    r = _invoke(runner, "flow", "business", "use", "--profile-url", _PROFILE_URL)
    assert r.exit_code == 0, r.stdout
    assert _stdout_json(r)["phase"] == "BUSINESS_SELECTED"

    r2 = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    assert r2.exit_code == 1
    assert _stdout_json(r2)["data"]["error"]["code"] == "calendar_gate_not_completed"


def test_less_direct_pick_needs_user_confirmation_before_selection(runner, isolated_session):
    from booking_agent_cli.session import load_session

    _init(runner)
    label = "installation deposit, not AC repair"
    args = ["flow", "business", "use", "--profile-url", _PROFILE_URL,
            "--service-id", _SERVICE_ID, "--less-direct", label]
    r = _invoke(runner, *args)
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["awaiting_user"] == "intent_mismatch_confirmation"
    assert label in body["prompt"]
    assert body["next_command"].endswith("--mismatch-confirmed")
    assert "--less-direct 'installation deposit, not AC repair'" in body["next_command"]
    assert load_session().flow_phase != "BUSINESS_SELECTED"

    r2 = _invoke(runner, *args, "--mismatch-confirmed")
    assert r2.exit_code == 0, r2.stdout + r2.stderr
    assert _stdout_json(r2)["phase"] == "BUSINESS_SELECTED"
    assert load_session().selection_mismatch == label


def test_business_use_stops_when_live_catalog_differs_from_search_row(runner, isolated_session):
    from booking_agent_cli.session import load_session, load_store, save_store

    _init(runner)
    store = load_store()
    store.sessions[0].discover_services = [{
        "service_id": _SERVICE_ID,
        "service_name": "AC Repair",
        "price_minor_units": 500,
        "price_display": "$5.00",
        "duration": "PT1H",
        "channel": {"type": "at_buyer_location"},
    }]
    save_store(store)

    args = ["flow", "business", "use", "--profile-url", _PROFILE_URL, "--service-id", _SERVICE_ID]
    r = _invoke(runner, *args)
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "service_details_changed"
    changes = {c["field"]: c for c in body["data"]["changes"]}
    assert changes["price"] == {"field": "price", "presented": "$5.00", "live_catalog": "$1.00"}
    assert changes["channel"] == {
        "field": "channel", "presented": "at_buyer_location", "live_catalog": "at_business_location",
    }
    assert body["next_command"].endswith("--details-confirmed")
    session = load_session()
    assert session.flow_phase != "BUSINESS_SELECTED"
    assert session.service_id is None

    r2 = _invoke(runner, *args, "--details-confirmed")
    assert r2.exit_code == 0, r2.stdout + r2.stderr
    assert _stdout_json(r2)["phase"] == "BUSINESS_SELECTED"


def test_business_use_keeps_the_searched_business_name_when_name_is_omitted(
    runner, isolated_session,
):
    from booking_agent_cli.session import load_session, load_store, save_store

    _init(runner)
    store = load_store()
    store.sessions[0].discover_services = [{
        "service_id": _SERVICE_ID,
        "business_name": "HVAC-Koby",
        "service_name": "AC Repair",
        "price_minor_units": 100,
        "price_display": "$1.00",
        "duration": "PT1H",
        "channel": {"type": "at_business_location"},
    }]
    save_store(store)

    r = _invoke(runner, "flow", "business", "use", "--profile-url", _PROFILE_URL,
                "--service-id", _SERVICE_ID)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert load_session().business.name == "HVAC-Koby"


def test_availability_fetches_live_catalog_before_query(runner, isolated_session, monkeypatch):
    """USP §6.3: GET /services/{id} runs before POST /availability/query."""
    import booking_agent_cli.commands.flow as flow

    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")

    backend = flow.resolve_backend()
    order: list[str] = []
    orig_get = backend.get_service
    orig_query = backend.query_availability

    async def tracked_get(profile, sid):
        order.append("get_service")
        return await orig_get(profile, sid)

    async def tracked_query(profile, sid, start_date, end_date, timezone=None):
        order.append("query_availability")
        return await orig_query(profile, sid, start_date, end_date, timezone)

    monkeypatch.setattr(backend, "get_service", tracked_get)
    monkeypatch.setattr(backend, "query_availability", tracked_query)

    body = _stdout_json(
        _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    )
    assert order == ["get_service", "query_availability"]
    live = body["data"]["live_service"]
    assert live["id"] == _SERVICE_ID
    assert live["name"] == "AC Repair"
    assert live["pricing"]["amount"] == 100
    assert live["pricing"]["currency"] == "USD"
    assert live["policies"]["confirmation_mode"] == "auto"


def test_availability_refuses_session_with_invalid_business_timezone(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.session import load_store, save_store

    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    store = load_store()
    store.sessions[0].business.timezone = "Mars/Olympus"
    save_store(store)

    backend = flow.resolve_backend()

    async def no_query(*_args, **_kwargs):
        raise AssertionError("availability must not be queried with an invalid business zone")

    monkeypatch.setattr(backend, "query_availability", no_query)

    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "business_timezone_invalid"


def _availability_without_stored_zone(runner, monkeypatch, response_tz, *args):
    """Query availability with no stored zone; the business answers with ``response_tz``."""
    import booking_agent_cli.commands.flow as flow
    from booking_agent.models.wire import AvailabilityResult
    from booking_agent_cli.session import load_store, save_store

    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    store = load_store()
    store.sessions[0].business.timezone = None
    save_store(store)

    backend = flow.resolve_backend()
    original = backend.query_availability
    calls = []

    async def answering(profile, service_id, start, end, tz=None):
        calls.append((start, end, tz))
        slots = await original(profile, service_id, start, end, tz)
        return AvailabilityResult(
            slots, business_timezone=response_tz, applied_date_bounds_timezone=response_tz,
        )

    monkeypatch.setattr(backend, "query_availability", answering)
    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID, *args)
    assert r.exit_code == 0, r.stdout
    return _stdout_json(r), calls


def test_availability_without_zone_sends_plain_dates_and_adopts_response_zone(
    runner, isolated_session, monkeypatch,
):
    from datetime import date, datetime, timedelta, timezone

    from booking_agent_cli.session import must_load

    body, calls = _availability_without_stored_zone(runner, monkeypatch, "Asia/Jerusalem")
    tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
    assert calls[0] == (tomorrow.isoformat(), (tomorrow + timedelta(days=6)).isoformat(), None)
    assert date.fromisoformat(calls[0][1]) - date.fromisoformat(calls[0][0]) == timedelta(days=6)
    data = body["data"]
    assert data["business_timezone"] == "Asia/Jerusalem"
    assert data["business_timezone_source"] == "availability_response"
    assert data["applied_date_bounds_timezone"] == "Asia/Jerusalem"
    assert "timezone_discrepancy" not in data
    assert must_load().business.timezone == "Asia/Jerusalem"

    listed = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    assert listed["data"]["slots"][0]["business_start_display"].endswith("(Asia/Jerusalem)")
    assert "did not name its timezone" not in listed["prompt"]


def test_availability_target_date_without_zone_sends_that_date_only(
    runner, isolated_session, monkeypatch,
):
    _, calls = _availability_without_stored_zone(
        runner, monkeypatch, "Asia/Jerusalem", "--target-date", "2026-11-02",
    )
    assert calls[0] == ("2026-11-02", "2026-11-02", None)


@pytest.mark.parametrize("response_tz", [None, "Mars/Olympus"])
def test_availability_without_any_business_zone_shows_slot_offsets(
    runner, isolated_session, monkeypatch, response_tz,
):
    from booking_agent_cli.session import must_load

    body, _ = _availability_without_stored_zone(runner, monkeypatch, response_tz)
    assert body["data"]["business_timezone"] is None
    assert "business_timezone_source" not in body["data"]
    assert must_load().business.timezone is None

    listed = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    assert listed["data"]["business_timezone_unknown"] is True
    assert listed["data"]["slots"][0]["business_start_display"].endswith("(UTC+00:00)")
    assert "did not name its timezone" in listed["prompt"]
    assert "timezone_fallback_note" not in listed["data"]

    _link_address(monkeypatch, with_address=False)
    recap = _select_first_and_resolve(runner, listed)
    assert recap["data"]["selected_slot"]["business_display"].endswith("(UTC+00:00)")
    assert "did not name its timezone" in recap["prompt"]


def test_availability_response_zone_replaces_a_different_stored_zone(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent.models.wire import AvailabilityResult
    from booking_agent_cli.session import must_load

    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    backend = flow.resolve_backend()
    original = backend.query_availability
    sent = []

    async def answering(profile, service_id, start, end, tz=None):
        sent.append(tz)
        slots = await original(profile, service_id, start, end, tz)
        return AvailabilityResult(slots, business_timezone="Asia/Jerusalem")

    monkeypatch.setattr(backend, "query_availability", answering)
    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    assert r.exit_code == 0, r.stdout
    data = _stdout_json(r)["data"]
    assert sent == ["America/Phoenix"]
    assert data["business_timezone"] == "Asia/Jerusalem"
    assert data["business_timezone_source"] == "availability_response"
    assert data["timezone_discrepancy"] == {
        "stored": "America/Phoenix", "availability_response": "Asia/Jerusalem",
    }
    assert must_load().business.timezone == "Asia/Jerusalem"


def test_availability_missing_response_zone_keeps_valid_stored_zone(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent.models.wire import AvailabilityResult
    from booking_agent_cli.session import must_load

    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    backend = flow.resolve_backend()
    original = backend.query_availability

    async def answering(profile, service_id, start, end, tz=None):
        slots = await original(profile, service_id, start, end, tz)
        return AvailabilityResult(slots, business_timezone="Mars/Olympus")

    monkeypatch.setattr(backend, "query_availability", answering)
    r = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    assert r.exit_code == 0, r.stdout
    data = _stdout_json(r)["data"]
    assert data["business_timezone"] == "America/Phoenix"
    assert "business_timezone_source" not in data
    assert must_load().business.timezone == "America/Phoenix"


def _set_mock_payment_timing(monkeypatch, timing):
    import booking_agent_cli.commands.flow as flow

    backend = flow.resolve_backend()
    original_get_service = backend.get_service

    async def get_service_with_timing(profile, service_id):
        service = await original_get_service(profile, service_id)
        policies = service.policies.model_copy(update={"payment_timing": timing})
        return service.model_copy(update={"policies": policies})

    monkeypatch.setattr(backend, "get_service", get_service_with_timing)
    return backend


@pytest.mark.parametrize(
    ("timing", "error_code"),
    [
        ("future_timing", "payment_timing_not_supported"),
    ],
)
def test_business_use_rejects_unsupported_payment_timing(
    runner, isolated_session, monkeypatch, timing, error_code,
):
    _init(runner)
    _set_mock_payment_timing(monkeypatch, timing)

    result = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        _PROFILE_URL,
        "--service-id",
        _SERVICE_ID,
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    assert _stdout_json(result)["data"]["error"]["code"] == error_code


@pytest.mark.parametrize(
    ("timing", "error_code"),
    [
        ("future_timing", "payment_timing_not_supported"),
    ],
)
def test_book_refetches_and_rejects_unsupported_payment_timing(
    runner, isolated_session, monkeypatch, timing, error_code,
):
    import booking_agent_cli.commands.flow as flow

    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    _invoke(runner, "flow", "buyer", "resolve")

    backend = _set_mock_payment_timing(monkeypatch, timing)
    booking_called = False
    original_create_booking = backend.create_booking

    async def tracked_create_booking(*args, **kwargs):
        nonlocal booking_called
        booking_called = True
        return await original_create_booking(*args, **kwargs)

    monkeypatch.setattr(backend, "create_booking", tracked_create_booking)
    result = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)

    assert result.exit_code == 1, result.stdout + result.stderr
    assert _stdout_json(result)["data"]["error"]["code"] == error_code
    assert booking_called is False


def _pin_calendar_host(monkeypatch, home, *, client_id: str | None) -> None:
    """Hide the developer machine's calendar token so gate tests are hermetic."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GOOGLECALENDAR_ACCESS_TOKEN", raising=False)
    if client_id:
        monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", client_id)
    else:
        monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)


def test_business_use_routes_at_service_directly(runner, isolated_session, monkeypatch):
    from booking_agent_cli.session import load_store, must_load

    _init(runner)
    _pin_calendar_host(monkeypatch, isolated_session, client_id=None)
    _set_mock_payment_timing(monkeypatch, "at_service")
    result = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        _PROFILE_URL,
        "--service-id",
        _SERVICE_ID,
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    body = _stdout_json(result)
    assert body["data"]["payment_route"] == "at_service"
    assert body["data"]["checkout_mode"] == "direct"
    # No calendar identity: the real gate auto-skips for this session only.
    assert body["next_command"] == "booking-agent flow availability"
    assert body["data"]["calendar_state"] == "skipped"
    assert "Next, ask about Google Calendar" not in body["prompt"]
    assert "direct" in body["data"]["supported_checkout_modes"]
    session = must_load()
    assert session.checkout_prereq_completed is True
    assert session.calendar_gate_completed is True
    assert load_store().last_calendar_state is None


def test_business_use_at_service_without_shopping_endpoint(
    runner, isolated_session, monkeypatch,
):
    """A USP-capable profile with no shopping binding still books at_service directly."""
    from booking_agent.ucp_profile import (
        parse_ucp_native_context_from_document,
        usp_business_profile_from_ucp_native,
    )

    doc = {
        "ucp": {
            "version": "2026-04-08",
            "capabilities": {
                "dev.usp-protocol.services.catalog": [{"version": "2026-08-20"}],
                "dev.usp-protocol.services.availability": [{"version": "2026-08-20"}],
                "dev.usp-protocol.services.bookings": [{"version": "2026-08-20"}],
            },
            "services": {
                "dev.usp-protocol.services": [
                    {
                        "version": "2026-08-20",
                        "transport": "rest",
                        "endpoint": "https://at-service.example/_api/usp-impl/v1",
                    }
                ],
            },
        },
        "business": {"name": "USP At Service", "timezone": "America/Phoenix"},
    }
    uctx = parse_ucp_native_context_from_document(doc, require_paid_bookings=False)
    profile = usp_business_profile_from_ucp_native(uctx)

    async def _resolve(_profile_url):
        return profile, uctx

    monkeypatch.setattr(
        "booking_agent.ucp_profile.fetch_and_resolve_business_profile",
        _resolve,
    )
    _init(runner)
    _pin_calendar_host(monkeypatch, isolated_session, client_id=None)
    _set_mock_payment_timing(monkeypatch, "at_service")

    result = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        "https://at-service.example/.well-known/ucp",
        "--service-id",
        _SERVICE_ID,
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    body = _stdout_json(result)
    assert body["data"]["payment_route"] == "at_service"
    assert body["data"]["checkout_mode"] == "direct"
    assert body["next_command"] == "booking-agent flow availability"
    assert body["data"]["calendar_state"] == "skipped"
    assert "Next, ask about Google Calendar" not in body["prompt"]
    assert "direct" in body["data"]["supported_checkout_modes"]
    assert "ucp_rest_base" not in body["data"]


def _install_ucp_business(monkeypatch, business: dict) -> None:
    from booking_agent.ucp_profile import (
        parse_ucp_native_context_from_document,
        usp_business_profile_from_ucp_native,
    )
    from support.mock.ucp_server import minimal_ucp_profile

    doc = minimal_ucp_profile(
        checkout_base="https://mock-ucp.test/checkout",
        usp_base="https://mock-ucp.test/usp",
    )
    doc["business"] = business
    uctx = parse_ucp_native_context_from_document(doc)
    profile = usp_business_profile_from_ucp_native(uctx)

    async def _resolve(_profile_url):
        return profile, uctx

    monkeypatch.setattr(
        "booking_agent.ucp_profile.fetch_and_resolve_business_profile",
        _resolve,
    )


def _seed_registry_timezone(timezone_name) -> None:
    from booking_agent_cli.session import load_store, save_store

    store = load_store()
    store.sessions[0].discover_services = [{
        "service_id": _SERVICE_ID,
        "registry_search_result": {"serviceId": _SERVICE_ID, "timezone": timezone_name},
    }]
    save_store(store)


def _business_use(runner):
    return _invoke(
        runner, "flow", "business", "use",
        "--profile-url", _PROFILE_URL, "--service-id", _SERVICE_ID,
    )


def test_business_use_records_profile_timezone(runner, isolated_session):
    from booking_agent_cli.session import must_load

    _init(runner)
    body = _complete_checkout_mode_setup(runner)
    session = must_load()
    assert session.business.timezone == "America/Phoenix"
    assert session.service_timezone is None
    assert body["data"]["business"]["timezone"] == "America/Phoenix"
    assert body["data"]["business_timezone"] == "America/Phoenix"
    assert "timezone_discrepancy" not in body["data"]


def test_business_use_falls_back_to_registry_timezone(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import must_load

    _install_ucp_business(monkeypatch, {"name": "No Zone", "currency": "USD"})
    _init(runner)
    _seed_registry_timezone("Asia/Jerusalem")
    r = _business_use(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    session = must_load()
    assert session.business.timezone == "Asia/Jerusalem"
    assert session.service_timezone == "Asia/Jerusalem"
    assert _stdout_json(r)["data"]["business_timezone_source"] == "registry_service"


def test_business_use_profile_timezone_wins_over_registry_and_reports_discrepancy(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import must_load

    _install_ucp_business(
        monkeypatch,
        {"name": "Zoned", "currency": "USD", "timezone": "Asia/Jerusalem"},
    )
    _init(runner)
    _seed_registry_timezone("America/Phoenix")
    r = _business_use(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    session = must_load()
    assert session.business.timezone == "Asia/Jerusalem"
    assert session.service_timezone == "America/Phoenix"
    data = _stdout_json(r)["data"]
    assert data["business_timezone_source"] == "business_profile"
    assert data["timezone_discrepancy"] == {
        "business_timezone": "Asia/Jerusalem",
        "service_timezone": "America/Phoenix",
    }


def test_business_use_invalid_profile_timezone_uses_valid_registry_zone(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import must_load

    _install_ucp_business(
        monkeypatch, {"name": "Bad Zone", "currency": "USD", "timezone": "Mars/Olympus"},
    )
    _init(runner)
    _seed_registry_timezone("America/Phoenix")
    r = _business_use(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert must_load().business.timezone == "America/Phoenix"


def test_business_use_without_any_timezone_continues_with_unknown_zone(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import must_load

    _install_ucp_business(monkeypatch, {"name": "No Zone", "currency": "USD"})
    _init(runner)
    r = _business_use(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["business_timezone"] is None
    session = must_load()
    assert session.flow_phase != "INIT"
    assert session.business.timezone is None


@pytest.mark.parametrize(
    ("profile_tz", "registry_tz"),
    [("Mars/Olympus", None), (None, "Not/AZone"), ("Mars/Olympus", "../etc/passwd")],
)
def test_business_use_with_only_invalid_timezones_fails_invalid(
    runner, isolated_session, monkeypatch, profile_tz, registry_tz,
):
    from booking_agent_cli.session import must_load

    business = {"name": "Bad Zone", "currency": "USD"}
    if profile_tz:
        business["timezone"] = profile_tz
    _install_ucp_business(monkeypatch, business)
    _init(runner)
    if registry_tz:
        _seed_registry_timezone(registry_tz)
    r = _business_use(runner)
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "business_timezone_invalid"
    assert must_load().flow_phase == "INIT"


def test_business_use_at_service_asks_for_calendar_consent(
    runner, isolated_session, monkeypatch,
):
    """A direct at_service booking must not record a calendar opt-out when consent is required."""
    from booking_agent_cli.session import load_store, must_load

    _init(runner)
    _pin_calendar_host(
        monkeypatch, isolated_session, client_id="operator-client-id"
    )
    _set_mock_payment_timing(monkeypatch, "at_service")

    result = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        _PROFILE_URL,
        "--service-id",
        _SERVICE_ID,
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    body = _stdout_json(result)
    assert body["data"]["payment_route"] == "at_service"
    assert body["data"]["checkout_mode"] == "direct"
    assert body["data"]["checkout_mode_reason"] == (
        "This service is booked directly. The quoted price is due at the appointment."
    )
    assert body["next_command"] == "booking-agent flow calendar ask"
    assert body.get("awaiting_user") != "calendar_consent"
    assert "Next, ask about Google Calendar" not in body["prompt"]
    assert "calendar skip" not in body["next_command"]
    session = must_load()
    assert session.checkout_mode == "direct"
    assert session.checkout_prereq_completed is True
    assert session.calendar_gate_completed is False
    assert session.calendar_state == "unset"
    assert load_store().last_calendar_state is None

    blocked = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    assert blocked.exit_code == 1
    assert _stdout_json(blocked)["data"]["error"]["code"] == "calendar_gate_not_completed"

    asked = _invoke(runner, "flow", "calendar", "ask")
    assert asked.exit_code == 0, asked.stdout + asked.stderr
    asked_body = _stdout_json(asked)
    assert asked_body["awaiting_user"] == "calendar_consent"
    assert must_load().calendar_state == "unset"
    assert load_store().last_calendar_state is None


def test_book_at_service_uses_direct_usp_booking(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.payment_helpers as payment_helpers

    _init(runner)
    _set_mock_payment_timing(monkeypatch, "at_service")
    r = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        _PROFILE_URL,
        "--service-id",
        _SERVICE_ID,
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    _invoke(runner, "flow", "buyer", "resolve")

    backend = flow.resolve_backend()
    checkout_called = False
    original_ucp = payment_helpers._create_booking_via_ucp_checkout

    async def tracked_ucp(*args, **kwargs):
        nonlocal checkout_called
        checkout_called = True
        return await original_ucp(*args, **kwargs)

    monkeypatch.setattr(payment_helpers, "_create_booking_via_ucp_checkout", tracked_ucp)
    result = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)
    assert result.exit_code == 0, result.stdout + result.stderr
    body = _stdout_json(result)
    assert body["data"]["payment_due"] == "at_service"
    assert body["data"]["checkout_path"] == "none"
    assert "continue_url" not in body["data"]
    assert body["data"]["payment_path_summary"] == (
        "Pay at appointment; no digital payment required"
    )
    assert body["next_command"] == "booking-agent flow book poll --wait"
    assert checkout_called is False


def test_book_rejects_changed_payment_timing(runner, isolated_session, monkeypatch):
    _init(runner)
    _set_mock_payment_timing(monkeypatch, "at_service")
    r = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        _PROFILE_URL,
        "--service-id",
        _SERVICE_ID,
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    _invoke(runner, "flow", "buyer", "resolve")
    _set_mock_payment_timing(monkeypatch, "at_booking")
    result = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)
    assert result.exit_code == 1, result.stdout + result.stderr
    assert _stdout_json(result)["data"]["error"]["code"] == "payment_timing_changed"


def test_book_without_slot_fails(runner, isolated_session):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    r = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "slot_not_selected"


def test_buyer_resolve_rejects_placeholder_email(runner, isolated_session, monkeypatch):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    body = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = body["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    mock_link_cli_user_info(monkeypatch, {"name": "Ada Lovelace", "email": "ada@example.com"})
    r = _invoke(runner, "flow", "buyer", "resolve")
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "buyer_email_invalid"


def test_buyer_resolve_emits_price_and_booking_recap(runner, isolated_session, monkeypatch):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    mock_link_cli_user_info(
        monkeypatch,
        {"name": "Ada Lovelace", "email": "ada@booking-agent.test", "phone": "+15551234567"},
    )
    body = _stdout_json(_invoke(runner, "flow", "buyer", "resolve"))
    assert body["phase"] == "BUYER_RESOLVED"
    assert body["data"]["buyer"]["name"] == "Ada Lovelace"
    assert body["data"]["price_display"] == "$1.00"
    assert body["data"]["amount_minor_units"] == 100
    assert body["data"]["selected_slot"]["slot_id"] == slot_id
    assert "combined confirmation" in body["prompt"].lower()


def test_full_mock_flow_through_payment_pending(runner, isolated_session, monkeypatch):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    body = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = body["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()

    mock_link_cli_user_info(
        monkeypatch, {"name": "Ada Lovelace", "email": "ada@booking-agent.test"}
    )
    r = _invoke(runner, "flow", "buyer", "resolve")
    assert r.exit_code == 0, r.stdout

    r = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)
    assert r.exit_code == 0, r.stdout
    body = _stdout_json(r)
    assert body["phase"] == "PAYMENT_PENDING"
    assert body["data"]["checkout_path"] == "ucp"
    assert "continue_url" not in body["data"]

    r = _invoke(runner, "flow", "validate-skill-compliance")
    assert r.exit_code == 0, r.stdout


def test_availability_with_connected_calendar_filters_conflicting_slots(
    runner, isolated_session, monkeypatch,
):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    skipped = _stdout_json(
        _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    )
    assert skipped["data"]["slot_count"] == 84
    assert skipped["data"]["calendar_filtered"] is False

    _invoke(runner, "flow", "reset")
    _init(runner, user_request="second run with calendar")
    _complete_checkout_mode_setup(runner)
    monkeypatch.setenv(
        "GOOGLE_CALENDAR_CLIENT_ID",
        "test-client-id.apps.googleusercontent.com",
    )
    connect = _invoke(runner, "flow", "calendar", "connect")
    assert connect.exit_code == 0, connect.stdout + connect.stderr
    connected = _stdout_json(
        _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    )
    assert connected["data"]["calendar_filtered"] is True
    assert connected["data"]["slots_filtered"] == 6
    assert connected["data"]["slot_count"] == 78


def test_validate_skill_compliance_rejects_skipped_gates(runner, isolated_session):
    _init(runner)
    r = _invoke(runner, "flow", "validate-skill-compliance")
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "skill_compliance_failed"


def test_lowlevel_search_does_not_create_session(runner, isolated_session):
    r = runner.invoke(app, ["search", "--query", "AC repair Yuma AZ"])
    assert r.exit_code == 0, r.stdout
    assert not (isolated_session / ".usp-session.json").exists()


def test_lowlevel_search_accepts_time_ranking_flags(runner, isolated_session):
    r = runner.invoke(
        app,
        [
            "search",
            "--query",
            "AC repair Yuma AZ",
            "--desired-from",
            "2026-09-01T12:00:00Z",
            "--no-prefer-sooner-availability-slots",
        ],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert not (isolated_session / ".usp-session.json").exists()


def test_cli_help_does_not_leak_shopping_agent_or_adk(runner):
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    lower = r.stdout.lower()
    assert "adk" not in lower
    assert "gemini" not in lower
    assert "shopping-agent" not in lower


# ---------------------------------------------------------------------------
# Session store — history, rotation, resume
# ---------------------------------------------------------------------------

def test_sessions_list_empty_store(runner, isolated_session):
    r = _invoke(runner, "flow", "sessions", "list")
    assert r.exit_code == 0
    body = _stdout_json(r)
    assert body["data"]["total"] == 0
    assert body["data"]["sessions"] == []


def test_sessions_list_shows_created_sessions(runner, isolated_session):
    _init(runner, user_request="first booking")
    _invoke(runner, "flow", "reset")
    _init(runner, user_request="second booking")

    r = _invoke(runner, "flow", "sessions", "list")
    assert r.exit_code == 0
    body = _stdout_json(r)
    assert body["data"]["total"] == 1  # first was reset (deleted)
    sessions = body["data"]["sessions"]
    assert sessions[0]["user_request"] == "second booking"
    assert sessions[0]["active"] is True


def test_sessions_list_newest_first(runner, isolated_session):
    from booking_agent_cli.session import load_store, save_store, get_active_session
    _init(runner, user_request="first booking")
    # Manually mark first session CONFIRMED so second init doesn't require --force
    store = load_store()
    get_active_session(store).flow_phase = "CONFIRMED"
    for i, s in enumerate(store.sessions):
        if s.id == store.active_session_id:
            store.sessions[i].flow_phase = "CONFIRMED"
    save_store(store)

    _init(runner, user_request="second booking")

    r = _invoke(runner, "flow", "sessions", "list")
    body = _stdout_json(r)
    assert body["data"]["total"] == 2
    # Newest first
    assert body["data"]["sessions"][0]["user_request"] == "second booking"
    assert body["data"]["sessions"][1]["user_request"] == "first booking"


def test_sessions_resume_switches_active_session(runner, isolated_session):
    from booking_agent_cli.session import load_store, save_store

    _init(runner, user_request="first booking")
    store = load_store()
    first_id = store.active_session_id
    # Mark CONFIRMED so second init is allowed without --force
    store.sessions[0].flow_phase = "CONFIRMED"
    save_store(store)

    _init(runner, user_request="second booking")

    # Resume the first session
    r = _invoke(runner, "flow", "sessions", "resume", "--session-id", first_id)
    assert r.exit_code == 0, r.stdout
    body = _stdout_json(r)
    assert body["data"]["session_id"] == first_id
    assert body["data"]["flow_phase"] == "CONFIRMED"

    # `flow status` should now reflect the first session
    r2 = _invoke(runner, "flow", "status")
    assert r2.exit_code == 0
    assert _stdout_json(r2)["phase"] == "CONFIRMED"


def test_sessions_resume_unknown_id_fails(runner, isolated_session):
    _init(runner)
    r = _invoke(runner, "flow", "sessions", "resume", "--session-id", "nonexistent-id")
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "session_not_found"


def test_session_store_rotation(runner, isolated_session):
    """Store never grows beyond max_sessions; oldest are dropped."""
    import os
    from booking_agent_cli.session import load_store, save_store

    # Set a tiny cap via env var (monkeypatch already set BOOKING_AGENT_SESSION_DIR)
    import booking_agent_cli.session as sess_mod
    original = sess_mod.DEFAULT_MAX_SESSIONS

    # Use monkeypatch indirectly: write an initial store with max_sessions=3
    store = load_store()
    store.max_sessions = 3
    save_store(store)

    for i in range(4):
        # Each init needs --force or a completed prior session; easiest: force
        r = runner.invoke(app, [
            "flow", "init",
            "--user-request", f"booking {i}",
            "--force",
        ])
        assert r.exit_code == 0, r.stdout

    store = load_store()
    assert len(store.sessions) == 3
    user_requests = [s.user_request for s in store.sessions]
    assert "booking 0" not in user_requests
    assert "booking 3" in user_requests


def test_session_migration_from_v1(runner, isolated_session):
    """A schema-version-1 single-object file is transparently migrated to v2."""
    import json
    from pathlib import Path
    from booking_agent_cli.session import session_path, load_store

    v1_data = {
        "schema_version": 1,
        "flow_phase": "SLOTS_PRESENTED",
        "secrets_mode": "demo",
        "business": {"profile_url": "https://example.com/.well-known/usp", "rest_endpoint": None, "name": "Test Biz"},
        "calendar_gate_completed": True,
        "calendar_state": "skipped",
        "availability_window": {"from": None, "to": None},
        "slots_presented": [],
        "slot_id": None,
        "slot_user_confirmed": False,
        "buyer_source": "none",
        "buyer": {"name": "", "email": "", "phone": None},
        "booking": {"id": None, "checkout_path": None, "continue_url": None, "payment_context": None},
        "payment": {"status": None, "spend_request_id": None},
        "last_error": None,
    }
    session_path().write_text(json.dumps(v1_data))

    store = load_store()
    assert store.schema_version == 2
    assert len(store.sessions) == 1
    assert store.sessions[0].flow_phase == "SLOTS_PRESENTED"
    assert store.sessions[0].business.name == "Test Biz"
    assert store.sessions_created == 1


def test_reset_removes_only_active_session(runner, isolated_session):
    from booking_agent_cli.session import load_store, save_store

    _init(runner, user_request="first booking")
    store = load_store()
    store.sessions[0].flow_phase = "CONFIRMED"
    save_store(store)

    _init(runner, user_request="second booking")
    store = load_store()
    assert len(store.sessions) == 2

    _invoke(runner, "flow", "reset")
    store = load_store()
    assert len(store.sessions) == 1
    assert store.sessions[0].user_request == "first booking"


def test_session_has_user_request_and_timestamps(runner, isolated_session):
    _init(runner, user_request="Book a haircut downtown")
    from booking_agent_cli.session import load_session
    s = load_session()
    assert s.user_request == "Book a haircut downtown"
    assert s.created_at  # non-empty ISO 8601 string
    assert s.id          # non-empty UUID string


# ---------------------------------------------------------------------------
# payment-pending session helpers
# ---------------------------------------------------------------------------

def _advance_to_payment_pending(runner, isolated_session, monkeypatch):
    """Drive the session to PAYMENT_PENDING using the mock backend."""
    from booking_agent_cli.session import load_store, save_store, get_active_session, BookingRef, PaymentRef

    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")

    # Manually put session into PAYMENT_PENDING with a booking
    store = load_store()
    session = get_active_session(store)
    session.flow_phase = "PAYMENT_PENDING"
    session.booking = BookingRef(
        id="bkg_test123",
        checkout_path="ucp",
        payment_context={"amountDue": 100, "currency": "USD"},
        action_expires_at=None,  # will be overwritten per test
    )
    session.payment = PaymentRef(status="pending")
    for i, s in enumerate(store.sessions):
        if s.id == session.id:
            store.sessions[i] = session
    save_store(store)
    return session


# ---------------------------------------------------------------------------
# prereq
# ---------------------------------------------------------------------------

def test_prereq_check_registry_only(runner, isolated_session):
    _init(runner)
    r = _invoke(runner, "flow", "prereq", "check")
    assert r.exit_code == 0, r.stdout
    body = _stdout_json(r)
    assert body["phase"] == "PREREQ_OK"
    assert "registry_url" in body["data"]
    assert "browser_channel" not in body["data"]


def test_prereq_check_uses_default_registry_without_env(
    runner, isolated_session, monkeypatch
):
    from booking_agent.registry_url import DEFAULT_REGISTRY_URL
    import booking_agent_cli.commands.flow as flow

    monkeypatch.delenv("USP_REGISTRY_URL", raising=False)
    monkeypatch.setattr(flow, "_probe_registry", lambda url: url == DEFAULT_REGISTRY_URL)

    _init(runner)
    r = _invoke(runner, "flow", "prereq", "check")

    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["data"]["registry_url"] == DEFAULT_REGISTRY_URL


# Yuma, AZ — used by flow/discover location tests
YUMA_LOCATION = "32.6927,-114.6277"


def test_discover_defaults_to_session_user_request(runner, isolated_session):
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(runner, "flow", "discover")
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["discover_query"] == "Book AC repair in Yuma AZ"
    assert "discover_location" not in body["data"]
    store = load_store()
    assert store.sessions[0].discover_query == "Book AC repair in Yuma AZ"
    assert store.sessions[0].discover_location_lat is None
    assert store.sessions[0].discover_registry_url
    assert store.sessions[0].discover_registry_request_body["query"] == "Book AC repair in Yuma AZ"
    assert "location" not in store.sessions[0].discover_registry_request_body
    assert "desired_service_time_ranges" not in store.sessions[0].discover_registry_request_body
    assert body["data"]["services"]


def test_discover_prompt_labels_less_direct_results_without_reordering(
    runner, isolated_session,
):
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(runner, "flow", "discover")
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    prompt = body["prompt"]
    assert "exact order" in prompt
    assert "Never omit, filter, or reorder" in prompt
    assert "less-direct alternative" in prompt
    assert "at_business_location" in prompt
    stored = load_store().sessions[0].discover_services
    assert [s["service_id"] for s in body["data"]["services"]] == [
        s["service_id"] for s in stored
    ]
    assert all("less_direct" not in s for s in body["data"]["services"])


def test_discover_persists_and_echoes_time_ranking_context(runner, isolated_session):
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(
        runner,
        "flow",
        "discover",
        "--desired-at",
        "2026-09-01T12:00:00Z",
        "--desired-from",
        "2026-09-02T09:00:00+00:00",
        "--desired-range",
        "2026-09-03T09:00:00Z,2026-09-03T17:00:00Z",
        "--no-prefer-sooner-availability-slots",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    expected = [
        {"at": "2026-09-01T12:00:00Z"},
        {"start": "2026-09-02T09:00:00+00:00"},
        {
            "start": "2026-09-03T09:00:00Z",
            "end": "2026-09-03T17:00:00Z",
        },
    ]
    assert body["data"]["desired_service_time_ranges"] == expected
    assert body["data"]["prefer_sooner_availability_slots"] is False
    request = load_store().sessions[0].discover_registry_request_body
    assert request["desired_service_time_ranges"] == expected
    assert request["prefer_sooner_availability_slots"] is False


@pytest.mark.parametrize(
    "args",
    [
        ["--desired-at", "not-a-date"],
        ["--desired-from", "2026-09-01T12:00:00"],
        ["--desired-range", "2026-09-01T12:00:00Z"],
        [
            "--desired-range",
            "2026-09-02T12:00:00Z,2026-09-01T12:00:00Z",
        ],
    ],
)
def test_discover_rejects_invalid_time_preferences(
    runner, isolated_session, args
):
    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(runner, "flow", "discover", *args)
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == (
        "discover_time_preference_invalid"
    )


def _confirm_yuma_area(runner, monkeypatch):
    import booking_agent_cli.geocode as geo
    from support.mock_link_cli import YUMA_SHIPPING_ADDRESS

    monkeypatch.setattr(
        geo, "_geocode_http",
        lambda _p: [{"lat": "32.6927", "lon": "-114.6277", "display_name": "Yuma"}],
    )
    mock_link_cli_user_info(
        monkeypatch, {"name": "Ada Lovelace", "email": "ada@booking-agent.test"},
        shipping_addresses=[YUMA_SHIPPING_ADDRESS],
    )
    _invoke(runner, "flow", "search-area", "propose")
    r = _invoke(runner, "flow", "search-area", "confirm", "--user-confirmed")
    assert r.exit_code == 0, r.stdout


def test_discover_uses_the_confirmed_area(runner, isolated_session, monkeypatch):
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    _confirm_yuma_area(runner, monkeypatch)
    r = _invoke(runner, "flow", "discover", "--radius-km", "40")
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["discover_location"] == {"lat": 32.6927, "lng": -114.6277, "radius_km": 40.0}
    assert body["data"]["search_area"] == "100 Test Street, Yuma AZ 85364"
    location = load_store().sessions[0].discover_registry_request_body["location"]
    assert location["radiusKm"] == 40.0


def test_discover_with_confirmed_area_needs_a_radius(runner, isolated_session, monkeypatch):
    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    _confirm_yuma_area(runner, monkeypatch)
    r = _invoke(runner, "flow", "discover")
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "discover_radius_required"


def test_place_search_without_a_confirmed_area_is_refused(runner, isolated_session):
    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(runner, "flow", "discover", "--radius-km", "25")
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "search_area_unconfirmed"
    assert body["next_command"] == "booking-agent flow search-area propose"


def test_discover_rows_carry_a_display_line_with_the_business(runner, isolated_session):
    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    body = _stdout_json(_invoke(runner, "flow", "discover"))
    for row in body["data"]["services"]:
        assert row["display_line"].startswith(f"{row['service_name']} by {row['business_name']}")
    assert "display_line verbatim" in body["prompt"]


def test_discover_display_line_parts():
    from booking_agent_cli.catalog import discover_display_line, duration_display

    assert duration_display("PT1H30M") == "1 hr 30 min"
    assert duration_display("PT45M") == "45 min"
    assert duration_display("P1D") is None
    assert discover_display_line({
        "service_name": "AC Repair", "business_name": "HVAC-Koby", "price_display": "$1.00",
        "duration": "PT1H", "channel": {"type": "at_buyer_location"},
    }) == "AC Repair by HVAC-Koby, $1.00, 1 hr, comes to your address"
    assert discover_display_line({
        "service_name": "Haircut", "business_name": "Salon", "duration": "PT30M",
        "channel": {"type": "at_business_location"},
        "locations": [{"address": "1 Main St, Yuma AZ"}],
        "location_facts": {"distance_km": 3.2},
    }) == "Haircut by Salon, 30 min, at the business, 1 Main St, Yuma AZ, 3.2 km away"


def test_discover_optional_location(runner, isolated_session):
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(runner, "flow", "discover", "--location", YUMA_LOCATION)
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["discover_location"]["lat"] == 32.6927
    store = load_store()
    assert store.sessions[0].discover_location_lat == 32.6927
    assert store.sessions[0].discover_registry_request_body["location"]["coordinates"]["lat"] == 32.6927
    assert "radiusKm" not in store.sessions[0].discover_registry_request_body["location"]


def test_discover_sends_radius_km_when_provided(runner, isolated_session):
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(
        runner,
        "flow",
        "discover",
        "--location",
        YUMA_LOCATION,
        "--radius-km",
        "25",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["discover_location"]["radius_km"] == 25.0
    store = load_store()
    assert store.sessions[0].discover_location_radius_km == 25.0
    assert store.sessions[0].discover_registry_request_body["location"]["radiusKm"] == 25.0


def test_discover_sends_geo_and_time_preferences_together(runner, isolated_session):
    """Geo filters and availability ranking context must coexist in one search body."""
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(
        runner,
        "flow",
        "discover",
        "--location",
        YUMA_LOCATION,
        "--radius-km",
        "25",
        "--desired-at",
        "2026-09-01T12:00:00Z",
        "--desired-range",
        "2026-09-03T09:00:00Z,2026-09-03T17:00:00Z",
        "--no-prefer-sooner-availability-slots",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    expected_ranges = [
        {"at": "2026-09-01T12:00:00Z"},
        {
            "start": "2026-09-03T09:00:00Z",
            "end": "2026-09-03T17:00:00Z",
        },
    ]
    assert body["data"]["discover_location"]["lat"] == 32.6927
    assert body["data"]["discover_location"]["radius_km"] == 25.0
    assert body["data"]["desired_service_time_ranges"] == expected_ranges
    assert body["data"]["prefer_sooner_availability_slots"] is False

    session = load_store().sessions[0]
    assert session.discover_location_radius_km == 25.0
    request = session.discover_registry_request_body
    assert request["location"]["coordinates"]["lat"] == 32.6927
    assert request["location"]["coordinates"]["lng"] == -114.6277
    assert request["location"]["radiusKm"] == 25.0
    assert request["desired_service_time_ranges"] == expected_ranges
    assert request["prefer_sooner_availability_slots"] is False


def test_discover_geo_and_time_preferences_omit_prefer_sooner_when_true(
    runner, isolated_session
):
    """Explicit --prefer-sooner-availability-slots stays out of the body (default)."""
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(
        runner,
        "flow",
        "discover",
        "--location",
        YUMA_LOCATION,
        "--radius-km",
        "10",
        "--desired-from",
        "2026-09-02T09:00:00+00:00",
        "--prefer-sooner-availability-slots",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["prefer_sooner_availability_slots"] is True

    request = load_store().sessions[0].discover_registry_request_body
    assert request["location"]["radiusKm"] == 10.0
    assert request["desired_service_time_ranges"] == [
        {"start": "2026-09-02T09:00:00+00:00"}
    ]
    assert "prefer_sooner_availability_slots" not in request


def test_discover_omits_verticals_unless_flag_given(runner, isolated_session):
    from booking_agent_cli.session import load_store

    _init(runner, user_request="Book a yoga class in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(runner, "flow", "discover")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "verticals" not in load_store().sessions[0].discover_registry_request_body

    r = _invoke(
        runner, "flow", "discover", "--vertical", "group", "--vertical", "appointment",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    request = load_store().sessions[0].discover_registry_request_body
    assert request["verticals"] == ["group", "appointment"]


def test_discover_rejects_unknown_vertical(runner, isolated_session):
    _init(runner, user_request="Book a yoga class in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(runner, "flow", "discover", "--vertical", "class")
    assert r.exit_code != 0
    assert _stdout_json(r)["data"]["error"]["code"] == "discover_vertical_invalid"


def test_lowlevel_search_combines_geo_and_time_ranking_flags(
    runner, isolated_session, monkeypatch
):
    """The low-level search builds one intent carrying geo and time preferences."""
    import booking_agent.headless as headless
    from booking_agent.backend.live import DEFAULT_REGISTRY_SEARCH_LIMIT
    from booking_agent.rest_client import build_registry_search_body

    captured = []
    original_build_search_intent = headless.build_search_intent

    def _record(*args, **kwargs):
        intent = original_build_search_intent(*args, **kwargs)
        captured.append(intent)
        return intent

    monkeypatch.setattr(headless, "build_search_intent", _record)

    r = runner.invoke(
        app,
        [
            "search",
            "--query",
            "AC repair Yuma AZ",
            "--location",
            YUMA_LOCATION,
            "--radius-km",
            "25",
            "--desired-at",
            "2026-09-01T12:00:00Z",
            "--no-prefer-sooner-availability-slots",
        ],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert not (isolated_session / ".usp-session.json").exists()
    assert len(captured) == 1
    intent = captured[0]
    request = build_registry_search_body(
        intent.query,
        location=intent.location,
        desired_service_time_ranges=intent.desired_service_time_ranges,
        prefer_sooner_availability_slots=intent.prefer_sooner_availability_slots,
        limit=DEFAULT_REGISTRY_SEARCH_LIMIT,
    )
    assert request["location"]["coordinates"]["lat"] == 32.6927
    assert request["location"]["radiusKm"] == 25.0
    assert request["desired_service_time_ranges"] == [{"at": "2026-09-01T12:00:00Z"}]
    assert request["prefer_sooner_availability_slots"] is False


def test_discover_rejects_invalid_location(runner, isolated_session):
    _init(runner, user_request="Book AC repair in Yuma AZ")
    _invoke(runner, "flow", "prereq", "check")
    r = _invoke(runner, "flow", "discover", "--location", "not-valid")
    assert r.exit_code != 0
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "discover_location_invalid"


def test_use_ucp_checkout_mode_fails_closed(runner, isolated_session, monkeypatch):
    from booking_agent_cli.session import load_session, save_session, Business

    _init(runner)
    session = load_session()
    assert session is not None
    session.business = Business(profile_url=_PROFILE_URL, name="HVAC Koby")
    session.checkout_mode = "ucp_native"
    session.supported_checkout_modes = ["ucp_native"]
    session.flow_phase = "BUSINESS_SELECTED"
    save_session(session)
    import booking_agent_cli.checkout_prereq as _prereq

    def _boom(*args, **kwargs):
        raise _prereq.PrereqFailure(
            code="ucp_platform_profile_invalid",
            message="forced failure for fallback test",
        )

    monkeypatch.setattr("booking_agent_cli.commands.flow.run_prereq_check_ucp", _boom)

    r = _invoke(runner, "flow", "use-ucp-checkout-mode")
    assert r.exit_code == 1, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "ucp_platform_profile_invalid"
    assert "redirect" not in (body.get("next_command") or "")


def test_business_use_fails_closed_for_non_ucp_profile(
    runner, isolated_session, monkeypatch,
):
    import booking_agent.ucp_profile as ucp_profile
    import booking_agent_cli.commands.flow as flow

    backend = flow.resolve_backend()

    async def resolve_non_ucp(profile_url):
        return await backend.resolve_profile(profile_url), None

    monkeypatch.setattr(
        ucp_profile,
        "fetch_and_resolve_business_profile",
        resolve_non_ucp,
    )
    _init(runner)
    result = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        _PROFILE_URL,
        "--service-id",
        _SERVICE_ID,
    )

    assert result.exit_code == 1, result.stdout + result.stderr
    body = _stdout_json(result)
    assert body["data"]["error"]["code"] == "ucp_native_required"
    assert "redirect" not in (body.get("next_command") or "")


def test_calendar_skip_requires_checkout_prereq(runner, isolated_session):
    from booking_agent_cli.session import load_session, save_session, Business

    _init(runner)
    session = load_session()
    assert session is not None
    session.business = Business(profile_url=_PROFILE_URL, name="HVAC Koby")
    session.checkout_mode = "ucp_native"
    session.supported_checkout_modes = ["ucp_native"]
    session.flow_phase = "BUSINESS_SELECTED"
    session.checkout_prereq_completed = False
    save_session(session)

    r = _invoke(runner, "flow", "calendar", "skip")
    assert r.exit_code == 1, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "checkout_prereq_not_completed"
    assert body["next_command"] == "booking-agent flow use-ucp-checkout-mode"


def test_pay_request_uses_live_catalog_on_ucp_native_when_registry_differs(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult
    from booking_agent_cli.session import BookingRef, load_session, save_session

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.checkout_mode = "ucp_native"
    session.booking = BookingRef(
        id=session.booking.id,
        checkout_path="ucp",
        payment_context=None,
    )
    session.discover_services = [
        {
            "service_id": _SERVICE_ID,
            "price_display": "$1.00",
            "currency": "USD",
            "registry_search_result": {
                "pricing": {
                    "amountMinorUnits": 100,
                    "currency": "USD",
                    "display": "$1.00",
                },
            },
        }
    ]
    session.live_service = {
        "name": "AC Repair",
        "pricing": {
            "amount_minor_units": 10000,
            "price_display": "$100.00",
            "currency": "USD",
        },
    }
    session.ucp_checkout_amount_minor = 10000
    session.ucp_spt_network_id = "net_test"
    save_session(session)

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SpendRequestCreateResult(
            id="lsrq_live_catalog_price",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_live_catalog_price"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        "x" * 100,
        "--no-then-complete",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert captured["amount"] == 10000


def test_pay_request_routes_next_command(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    book_body = _drive_to_payment_pending(runner)

    def fake_create(**kwargs):
        assert kwargs.get("credential_type") == "shared_payment_token"
        return SpendRequestCreateResult(
            id="lsrq_test",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_test"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
        "--no-then-complete",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["spend_request_id"] == "lsrq_test"
    assert "pay poll" in body["next_command"]


def test_pay_request_ucp_native_uses_session_network_id(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult
    from booking_agent_cli.session import load_session, save_session

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.checkout_mode = "ucp_native"
    session.ucp_spt_network_id = "ntwrk_from_checkout"
    save_session(session)

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SpendRequestCreateResult(
            id="lsrq_ucp",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_ucp"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
        "--no-then-complete",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert captured["credential_type"] == "shared_payment_token"
    assert captured["network_id"] == "ntwrk_from_checkout"
    assert "pay poll" in body["next_command"]


def test_pay_request_then_complete_emits_approval_url_before_blocking(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    _drive_to_payment_pending(runner)

    def fake_create(**kwargs):
        return SpendRequestCreateResult(
            id="lsrq_test",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_test"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    monkeypatch.setattr(flow, "_run_payment_completion", lambda *_a, **_k: None)

    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
        "--then-complete",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[0] == "https://link.test/approve"
    body = _stdout_json(r, index=0)
    assert body["data"]["approval_url"] == "https://link.test/approve"
    assert body["user_message"] == "https://link.test/approve"
    assert body["awaiting_user"] is None
    assert "Scripts/CI only" in body["prompt"]
    assert "approve spend request in Link:" not in r.stderr
    assert "show user_message from stdout" in r.stderr


def test_pay_request_then_complete_polls(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    _drive_to_payment_pending(runner)

    def fake_create(**kwargs):
        return SpendRequestCreateResult(
            id="lsrq_test",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_test"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    completion_called = {"value": False}

    def fake_completion(*_a, **_k):
        completion_called["value"] = True

    monkeypatch.setattr(flow, "_run_payment_completion", fake_completion)

    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
        "--then-complete",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert completion_called["value"] is True


def test_pay_request_default_returns_without_blocking(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    _drive_to_payment_pending(runner)

    def fake_create(**kwargs):
        return SpendRequestCreateResult(
            id="lsrq_test",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_test"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    completion_called = {"value": False}

    def fake_completion(*_a, **_k):
        completion_called["value"] = True

    monkeypatch.setattr(flow, "_run_payment_completion", fake_completion)

    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert completion_called["value"] is False
    assert r.stdout.splitlines()[0] == "https://link.test/approve"
    body = _stdout_json(r)
    assert body["user_message"] == "https://link.test/approve"
    assert body["awaiting_user"] == "link_spend_approval"
    assert "STOP" in body["prompt"]
    assert "in the background" in body["prompt"]
    assert "do NOT use run_in_background" not in body["prompt"]
    assert "in the foreground" not in body["prompt"]
    assert body["next_command"] is not None
    assert "pay poll" in body["next_command"]
    assert "approve spend request in Link:" not in r.stderr
    assert "show user_message from stdout" in r.stderr


def test_pay_request_no_then_complete_emits_stderr_approval_url(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    _drive_to_payment_pending(runner)

    def fake_create(**kwargs):
        return SpendRequestCreateResult(
            id="lsrq_test",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_test"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)

    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
        "--no-then-complete",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[0] == "https://link.test/approve"
    body = _stdout_json(r)
    assert body["data"]["approval_url"] == "https://link.test/approve"
    assert body["user_message"] == "https://link.test/approve"
    assert body["awaiting_user"] == "link_spend_approval"
    assert "STOP" in body["prompt"]
    assert "approve spend request in Link:" not in r.stderr
    assert "show user_message from stdout" in r.stderr


def test_pay_ucp_complete_blocked_without_ack_surface(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult
    from booking_agent_cli.session import load_session, save_session

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.checkout_mode = "ucp_native"
    session.booking.checkout_path = "ucp"
    session.ucp_checkout_session_id = "chk_test"
    session.ucp_spt_network_id = "ntwrk_test"
    save_session(session)

    def fake_create(**kwargs):
        return SpendRequestCreateResult(
            id="lsrq_gate",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_gate"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    monkeypatch.setattr(
        flow,
        "resolve_spend_approval_url",
        lambda _sr_id: "https://link.test/approve",
    )
    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
    )
    assert r.exit_code == 0, r.stdout + r.stderr

    r2 = _invoke(
        runner,
        "flow",
        "pay",
        "ucp-complete",
        "--spend-request-id",
        "lsrq_gate",
    )
    assert r2.exit_code == 1, r2.stdout + r2.stderr
    assert r2.stdout.splitlines()[0] == "https://link.test/approve"
    body = _stdout_json(r2)
    assert body["data"]["error"]["code"] == "approval_not_surfaced_to_user"
    assert body["next_command"] == "booking-agent flow pay poll"
    message = body["data"]["error"]["message"]
    assert "in the background" in message
    assert "End the turn" not in message


def test_pay_poll_completes_ucp_checkout(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent_cli.link_cli import SpendRequestCreateResult
    from booking_agent_cli.session import load_session, save_session

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.checkout_mode = "ucp_native"
    session.booking.checkout_path = "ucp"
    session.ucp_checkout_session_id = "chk_test"
    session.ucp_spt_network_id = "ntwrk_test"
    save_session(session)

    def fake_create(**kwargs):
        return SpendRequestCreateResult(
            id="lsrq_poll",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_poll"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)

    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert "pay poll" in _stdout_json(r)["next_command"]

    monkeypatch.setattr(flow, "_poll_spend_approval_or_exit", lambda *_a, **_k: None)

    async def fake_complete(*_a, **_k):
        return {
            "complete_checkout": {},
            "checkout_id": "chk_test",
            "order_id": "ord_1",
            "handler_id_resolved": "com.stripe",
        }

    monkeypatch.setattr(payment_helpers, "complete_ucp_checkout_async", fake_complete)

    r_poll = _invoke(runner, "flow", "pay", "poll")
    assert r_poll.exit_code == 0, r_poll.stdout + r_poll.stderr
    body = _stdout_json(r_poll)
    assert body["phase"] == "CONFIRMED"
    assert body["data"]["skill_compliance_validated"] is True
    assert "booked" in body["prompt"].lower()


def test_pay_ack_surface_enables_ucp_complete(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent_cli.link_cli import SpendRequestCreateResult
    from booking_agent_cli.session import load_session, save_session

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.checkout_mode = "ucp_native"
    session.booking.checkout_path = "ucp"
    session.ucp_checkout_session_id = "chk_test"
    session.ucp_spt_network_id = "ntwrk_test"
    save_session(session)

    def fake_create(**kwargs):
        return SpendRequestCreateResult(
            id="lsrq_ack",
            approval_url="https://link.test/approve",
            raw={"id": "lsrq_ack"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    monkeypatch.setattr(flow, "_poll_spend_approval_or_exit", lambda *_a, **_k: None)

    async def fake_complete(*_a, **_k):
        return {
            "complete_checkout": {},
            "checkout_id": "chk_test",
            "order_id": "ord_1",
            "handler_id_resolved": "com.stripe",
        }

    monkeypatch.setattr(payment_helpers, "complete_ucp_checkout_async", fake_complete)

    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
    )
    assert r.exit_code == 0, r.stdout + r.stderr

    r_ack = _invoke(runner, "flow", "pay", "ack-surface")
    assert r_ack.exit_code == 0, r_ack.stdout + r_ack.stderr
    ack = _stdout_json(r_ack)
    assert "pay ucp-complete" in ack["next_command"]

    r2 = _invoke(
        runner,
        "flow",
        "pay",
        "ucp-complete",
        "--spend-request-id",
        "lsrq_ack",
    )
    assert r2.exit_code == 0, r2.stdout + r2.stderr
    assert _stdout_json(r2)["phase"] == "CONFIRMED"


def test_flow_status_payment_pending_does_not_resurface_approval_url(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.link_cli import SpendRequestStatus
    from booking_agent_cli.session import load_session, save_session

    import booking_agent_cli.payment_helpers as payment_helpers

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.payment.spend_request_id = "lsrq_status"
    session.payment.spend_approval_surfaced = True
    session.payment.spend_approval_user_surfaced = True
    save_session(session)

    def fake_retrieve(sr_id: str) -> SpendRequestStatus:
        return SpendRequestStatus(
            id=sr_id,
            status="pending_approval",
            approval_url="https://link.test/status-approve",
        )

    monkeypatch.setattr(payment_helpers, "retrieve_spend_request", fake_retrieve)

    r = _invoke(runner, "flow", "status")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert not r.stdout.startswith("https://")
    body = _stdout_json(r)
    assert body["data"]["approval_url"] == "https://link.test/status-approve"
    assert body["user_message"] is None
    assert body["awaiting_user"] is None
    assert "foreground" in body["prompt"].lower()


def test_pay_ucp_complete_does_not_resurface_approval_url(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent_cli.session import load_session, save_session

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.checkout_mode = "ucp_native"
    session.booking.checkout_path = "ucp"
    session.ucp_checkout_session_id = "chk_test"
    session.payment.spend_request_id = "lsrq_ucp"
    session.payment.spend_approval_surfaced = True
    session.payment.spend_approval_user_surfaced = True
    save_session(session)

    monkeypatch.setattr(flow, "_poll_spend_approval_or_exit", lambda *_a, **_k: None)

    async def fake_complete(*_a, **_k):
        return {
            "complete_checkout": {},
            "checkout_id": "chk_test",
            "order_id": "ord_1",
            "handler_id_resolved": "com.stripe",
        }

    monkeypatch.setattr(payment_helpers, "complete_ucp_checkout_async", fake_complete)

    r = _invoke(
        runner,
        "flow",
        "pay",
        "ucp-complete",
        "--spend-request-id",
        "lsrq_ucp",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert not r.stdout.startswith("https://")
    body = _stdout_json(r)
    assert body["user_message"] is None
    assert body["phase"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# Issuance 3DS on the spend request (spec #93 A4)
# ---------------------------------------------------------------------------

_3DS_MESSAGE = "Your bank needs to verify this payment."
_3DS_URL = "https://app.link.com/3ds/verify"


def _next_action(action_type="three_d_secure", resolution="auto_resume"):
    from booking_agent_cli.link_cli import SpendNextAction

    return SpendNextAction(
        type=action_type,
        resolution=resolution,
        display_message=_3DS_MESSAGE,
        action_url=_3DS_URL,
    )


def _ucp_payment_pending(runner):
    from booking_agent_cli.session import load_session, save_session

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.checkout_mode = "ucp_native"
    session.booking.checkout_path = "ucp"
    session.ucp_checkout_session_id = "chk_test"
    session.ucp_spt_network_id = "ntwrk_test"
    save_session(session)


def _pay_request(runner, monkeypatch, created):
    import booking_agent_cli.commands.flow as flow

    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return created() if callable(created) else created

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    r = _invoke(
        runner, "flow", "pay", "request",
        "--payment-method-id", "pm_test", "--context", "x" * 100,
    )
    return r, calls


def test_pay_request_reads_context_from_stdin_without_shell_expansion(
    runner, isolated_session, monkeypatch,
):
    """A double-quoted "$1.00 deposit" reached Link as ".00 deposit" in the incident."""
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.main import app

    _ucp_payment_pending(runner)
    calls = []
    monkeypatch.setattr(
        flow, "create_spend_request",
        lambda **kw: calls.append(kw) or _approval_created("lsrq_ctx"),
    )
    text = "Pay the $1.00 deposit for AC repair at HVAC-Koby on Tue Oct 6, 4:00 PM IDT. " * 2
    r = runner.invoke(
        app,
        ["flow", "pay", "request", "--payment-method-id", "pm_test", "--context-file", "-"],
        input=text,
    )
    assert r.exit_code == 0, r.stdout
    assert calls[0]["context"] == text.strip()
    assert "$1.00" in calls[0]["context"]


def test_pay_request_writes_the_link_note_from_the_session(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    _ucp_payment_pending(runner)
    calls = []
    monkeypatch.setattr(
        flow, "create_spend_request",
        lambda **kw: calls.append(kw) or _approval_created("lsrq_auto"),
    )
    r = _invoke(runner, "flow", "pay", "request", "--payment-method-id", "pm_test")
    assert r.exit_code == 0, r.stdout
    note = calls[0]["context"]
    assert len(note) >= 100
    assert note.startswith("Booking ")
    assert "Total $1.00" in note


def test_pay_request_refuses_two_context_sources(runner, isolated_session):
    _ucp_payment_pending(runner)
    r = runner.invoke(
        app,
        ["flow", "pay", "request", "--payment-method-id", "pm_test",
         "--context", "x" * 120, "--context-file", "-"],
        input="y" * 120,
    )
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "context_required"


def _script_link_retrieve(monkeypatch, statuses):
    """Feed link_cli.wait_for_spend_approval a scripted sequence of retrieves."""
    import booking_agent_cli.link_cli as link_cli

    seen: list[str] = []
    queue = list(statuses)

    def fake_retrieve(sr_id):
        seen.append(sr_id)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(link_cli, "retrieve_spend_request", fake_retrieve)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return seen


def _fake_ucp_complete(monkeypatch):
    import booking_agent_cli.payment_helpers as payment_helpers

    completed = {"count": 0}

    async def fake_complete(*_a, **_k):
        completed["count"] += 1
        return {
            "complete_checkout": {},
            "checkout_id": "chk_test",
            "order_id": "ord_1",
            "handler_id_resolved": "stripe_payments",
        }

    monkeypatch.setattr(payment_helpers, "complete_ucp_checkout_async", fake_complete)
    return completed


def _three_ds_created():
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    return SpendRequestCreateResult(
        id="lsrq_3ds",
        approval_url=None,
        raw={"id": "lsrq_3ds", "status": "requires_action"},
        command_summary="link-cli spend-request create ...",
        status="requires_action",
        next_action=_next_action(),
    )


def _approval_created(sr_id="lsrq_new"):
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    return SpendRequestCreateResult(
        id=sr_id,
        approval_url="https://link.test/approve",
        raw={"id": sr_id},
        command_summary="link-cli spend-request create ...",
    )


def test_pay_request_surfaces_create_time_three_ds(runner, isolated_session, monkeypatch):
    from booking_agent_cli.session import load_session

    _ucp_payment_pending(runner)
    r, _ = _pay_request(runner, monkeypatch, _three_ds_created)

    assert r.exit_code == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[:2] == [_3DS_MESSAGE, _3DS_URL]
    body = _stdout_json(r)
    assert "error" not in body["data"]
    assert body["user_message"] == f"{_3DS_MESSAGE}\n{_3DS_URL}"
    assert body["awaiting_user"] == "link_spend_approval"
    assert body["next_command"] == "booking-agent flow pay poll"
    assert "not a payment failure" in body["prompt"]
    assert "in the background" in body["prompt"]
    assert "in the foreground" not in body["prompt"]
    assert body["data"]["next_action"]["type"] == "three_d_secure"
    session = load_session()
    assert session.payment.spend_request_id == "lsrq_3ds"
    assert session.payment.spend_action_surfaced == "lsrq_3ds:three_d_secure"
    assert session.payment.payment_method_id == "pm_test"


@pytest.mark.parametrize("final_status", ["submitted", "succeeded"])
def test_pay_poll_resumes_same_request_after_create_time_three_ds(
    runner, isolated_session, monkeypatch, final_status,
):
    from booking_agent_cli.link_cli import SpendRequestStatus

    _ucp_payment_pending(runner)
    _pay_request(runner, monkeypatch, _three_ds_created)
    seen = _script_link_retrieve(monkeypatch, [
        SpendRequestStatus(id="lsrq_3ds", status="requires_action", next_action=_next_action()),
        SpendRequestStatus(id="lsrq_3ds", status=final_status),
    ])
    completed = _fake_ucp_complete(monkeypatch)

    r = _invoke(runner, "flow", "pay", "poll")

    assert r.exit_code == 0, r.stdout + r.stderr
    # The action was already shown; resuming must not show it again.
    assert _3DS_URL not in r.stdout
    assert set(seen) == {"lsrq_3ds"}
    assert completed["count"] == 1
    assert _stdout_json(r)["phase"] == "CONFIRMED"


def test_pay_poll_surfaces_retrieve_time_three_ds_once(runner, isolated_session, monkeypatch):
    from booking_agent_cli.link_cli import SpendRequestStatus
    from booking_agent_cli.session import load_session

    _ucp_payment_pending(runner)
    _pay_request(runner, monkeypatch, _approval_created("lsrq_poll"))
    action_required = SpendRequestStatus(
        id="lsrq_poll", status="requires_action", next_action=_next_action(),
    )
    _script_link_retrieve(monkeypatch, [action_required])
    completed = _fake_ucp_complete(monkeypatch)

    r = _invoke(runner, "flow", "pay", "poll")

    assert r.exit_code == 0, r.stdout + r.stderr
    assert r.stdout.splitlines()[:2] == [_3DS_MESSAGE, _3DS_URL]
    body = _stdout_json(r)
    assert "error" not in body["data"]
    assert body["awaiting_user"] == "link_spend_approval"
    assert body["next_command"] == "booking-agent flow pay poll"
    assert completed["count"] == 0
    assert load_session().payment.spend_action_surfaced == "lsrq_poll:three_d_secure"

    _script_link_retrieve(monkeypatch, [
        action_required,
        SpendRequestStatus(id="lsrq_poll", status="submitted"),
    ])
    r2 = _invoke(runner, "flow", "pay", "poll")

    assert r2.exit_code == 0, r2.stdout + r2.stderr
    assert _3DS_URL not in r2.stdout
    assert completed["count"] == 1
    assert _stdout_json(r2)["phase"] == "CONFIRMED"


def test_three_ds_retry_requires_a_new_spend_request(runner, isolated_session, monkeypatch):
    from booking_agent_cli.link_cli import SpendRequestStatus
    from booking_agent_cli.session import load_session

    _ucp_payment_pending(runner)
    _pay_request(runner, monkeypatch, _three_ds_created)
    _script_link_retrieve(monkeypatch, [
        SpendRequestStatus(
            id="lsrq_3ds",
            status="requires_action",
            next_action=_next_action("three_d_secure_retry", "create_new_spend_request"),
        ),
    ])
    completed = _fake_ucp_complete(monkeypatch)

    r = _invoke(runner, "flow", "pay", "poll")

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "spend_request_replacement_required"
    assert body["user_message"] == f"{_3DS_MESSAGE}\n{_3DS_URL}"
    assert body["next_command"].startswith(
        "booking-agent flow pay request --payment-method-id pm_test"
    )
    assert completed["count"] == 0
    assert not load_session().payment.spend_approval_surfaced

    # The retired request cannot be continued.
    r_old = _invoke(runner, "flow", "pay", "poll")
    assert r_old.exit_code == 1
    assert _stdout_json(r_old)["data"]["error"]["code"] == "approval_not_created"

    # A fresh spend request is allowed and replaces the old id.
    r_new, calls = _pay_request(runner, monkeypatch, _approval_created("lsrq_new"))
    assert r_new.exit_code == 0, r_new.stdout + r_new.stderr
    assert len(calls) == 1
    assert load_session().payment.spend_request_id == "lsrq_new"


def test_pay_request_create_time_non_resumable_action_requires_new_request(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    _ucp_payment_pending(runner)
    created = SpendRequestCreateResult(
        id="lsrq_pm",
        approval_url=None,
        raw={"id": "lsrq_pm"},
        command_summary="link-cli spend-request create ...",
        status="requires_action",
        next_action=_next_action("update_payment_method", "create_new_spend_request"),
    )
    r, _ = _pay_request(runner, monkeypatch, created)

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "spend_request_replacement_required"
    assert "pay request" in body["next_command"]


def test_pay_poll_plain_approval_still_times_out(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestStatus

    _ucp_payment_pending(runner)
    _pay_request(runner, monkeypatch, _approval_created("lsrq_slow"))
    _script_link_retrieve(monkeypatch, [
        SpendRequestStatus(id="lsrq_slow", status="pending_approval"),
    ])
    monkeypatch.setattr(flow, "approval_poll_deadline", lambda *_a, **_k: 0.0)

    r = _invoke(runner, "flow", "pay", "poll")

    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "approval_timeout"


def test_ucp_complete_reports_an_approved_spend_with_no_token_as_not_issued(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent_cli.link_cli import SharedPaymentTokenUnavailable

    _ucp_payment_pending(runner)
    _pay_request(runner, monkeypatch, _approval_created("lsrq_nospt"))
    from booking_agent_cli.session import load_session, save_session

    session = load_session()
    session.payment.spend_approval_user_surfaced = True
    save_session(session)
    monkeypatch.setattr(flow, "_poll_spend_approval_or_exit", lambda *a, **k: None)

    async def no_token(session, **kwargs):
        raise SharedPaymentTokenUnavailable("lsrq_nospt", "approved", 30)

    monkeypatch.setattr(payment_helpers, "complete_ucp_checkout_async", no_token)

    r = _invoke(runner, "flow", "pay", "ucp-complete", "--spend-request-id", "lsrq_nospt")

    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "shared_payment_token_not_issued"
    assert "approval went through" in body["user_message"]
    assert "haven't seen your approval" not in body["user_message"]


def test_pay_request_ucp_native_missing_network_id(runner, isolated_session):
    from booking_agent_cli.session import load_session, save_session

    _drive_to_payment_pending(runner)
    session = load_session()
    assert session is not None
    session.checkout_mode = "ucp_native"
    session.ucp_spt_network_id = None
    save_session(session)

    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
        "--no-then-complete",
    )
    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "missing_network_id_for_spt"
    assert "pay rebook" in body["next_command"]


def test_pay_request_help_has_no_network_id_flag(runner):
    r = runner.invoke(
        __import__("booking_agent_cli.main", fromlist=["app"]).app,
        ["flow", "pay", "request", "--help"],
    )
    assert r.exit_code == 0
    assert "--network-id" not in r.stdout


def _set_deposit_disclosures(*, disclosures):
    from booking_agent_cli.session import get_active_session, load_store, save_store

    store = load_store()
    session = get_active_session(store)
    session.payment_route = "deposit_required"
    session.due_now_amount_minor = 1000
    session.ucp_checkout_amount_minor = 10000
    session.payment_schedules = [
        {"id": "now", "type": "immediate", "amount": 1000},
        {"id": "later", "type": "at_service", "amount": 9000},
    ]
    session.payment_disclosures = disclosures
    for i, existing in enumerate(store.sessions):
        if existing.id == session.id:
            store.sessions[i] = session
    save_store(store)


def test_pay_request_surfaces_deposit_disclosures_before_spend(
    runner, isolated_session, monkeypatch,
):
    _advance_to_payment_pending(runner, isolated_session, monkeypatch)
    disclosure = {
        "presentation": "disclosure",
        "code": "balance_due_at_service",
        "description": {"plain": "The remaining balance is due at the appointment."},
    }
    _set_deposit_disclosures(disclosures=[disclosure])

    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        "x" * 100,
    )

    assert r.exit_code == 1, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "disclosures_unconfirmed"
    assert body["data"]["due_now_amount_minor"] == 1000
    assert body["data"]["checkout_total_minor"] == 10000
    assert body["data"]["schedules"] == [
        {"id": "now", "type": "immediate", "amount": 1000},
        {"id": "later", "type": "at_service", "amount": 9000},
    ]
    assert body["data"]["disclosures"] == [disclosure]
    assert "--disclosures-confirmed" in body["next_command"]


def test_pay_request_fails_closed_for_unrenderable_disclosure(
    runner, isolated_session, monkeypatch,
):
    _advance_to_payment_pending(runner, isolated_session, monkeypatch)
    from booking_agent_cli.session import get_active_session, load_store, save_store

    store = load_store()
    session = get_active_session(store)
    session.ucp_checkout_links = [{"rel": "continue", "url": "https://merchant.test/terms"}]
    save_store(store)
    disclosure = {"presentation": "disclosure", "code": "merchant_notice"}
    _set_deposit_disclosures(disclosures=[disclosure])

    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        "x" * 100,
        "--disclosures-confirmed",
    )

    assert r.exit_code == 1, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "disclosure_unrenderable"
    assert body["data"]["continue_url"] == "https://merchant.test/terms"
    assert body["data"]["disclosures"] == [disclosure]


def test_pay_request_persists_disclosure_confirmation_before_retry(
    runner, isolated_session, monkeypatch,
):
    _advance_to_payment_pending(runner, isolated_session, monkeypatch)
    _set_deposit_disclosures(disclosures=[])

    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        "x" * 100,
        "--disclosures-confirmed",
    )

    assert r.exit_code == 1, r.stdout + r.stderr
    assert _stdout_json(r)["data"]["error"]["code"] == "missing_network_id_for_spt"
    from booking_agent_cli.session import load_session

    assert load_session().disclosures_user_confirmed is True


def _set_taxed_quote(*, total=110, line_item=100, confirmed=None):
    from booking_agent_cli.session import load_session, save_session

    session = load_session()
    session.ucp_checkout_amount_minor = total
    session.ucp_merchant_quote = {
        "currency": "USD",
        "total_minor_units": total,
        "tax_minor_units": total - line_item,
        "subtotal_minor_units": line_item,
        "line_item_price_minor_units": line_item,
    }
    session.total_confirmed_amount_minor = confirmed
    save_session(session)


def test_pay_request_refuses_a_taxed_total_the_user_has_not_approved(
    runner, isolated_session, monkeypatch,
):
    _ucp_payment_pending(runner)
    _set_taxed_quote()

    r, calls = _pay_request(runner, monkeypatch, _approval_created("lsrq_tax"))

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "checkout_total_unconfirmed"
    quote = body["data"]["merchant_quote"]
    assert quote["total_display"] == "$1.10"
    assert quote["tax_display"] == "$0.10"
    assert quote["line_item_display"] == "$1.00"
    assert quote["total_changed"] is True
    assert "--total-confirmed" in body["next_command"]
    assert calls == []


def test_pay_request_charges_the_taxed_total_after_confirmation(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.session import load_session

    _ucp_payment_pending(runner)
    _set_taxed_quote()
    calls = []
    monkeypatch.setattr(
        flow, "create_spend_request",
        lambda **kw: calls.append(kw) or _approval_created("lsrq_tax"),
    )

    r = _invoke(
        runner, "flow", "pay", "request",
        "--payment-method-id", "pm_test", "--context", "x" * 100, "--total-confirmed",
    )

    assert r.exit_code == 0, r.stdout
    assert calls[0]["amount"] == 110
    assert load_session().total_confirmed_amount_minor == 110


def test_pay_request_needs_no_total_confirmation_when_total_matches_quote(
    runner, isolated_session, monkeypatch,
):
    _ucp_payment_pending(runner)
    _set_taxed_quote(total=100, line_item=100)

    r, calls = _pay_request(runner, monkeypatch, _approval_created("lsrq_flat"))

    assert r.exit_code == 0, r.stdout
    assert calls[0]["amount"] == 100


def test_pay_request_stale_total_confirmation_does_not_cover_a_new_total(
    runner, isolated_session, monkeypatch,
):
    _ucp_payment_pending(runner)
    _set_taxed_quote(total=120, line_item=100, confirmed=110)

    r, calls = _pay_request(runner, monkeypatch, _approval_created("lsrq_stale"))

    assert r.exit_code == 1, r.stdout
    assert _stdout_json(r)["data"]["error"]["code"] == "checkout_total_unconfirmed"
    assert calls == []

# ---------------------------------------------------------------------------
# booking and payment integration (mock server + mocked link-cli)
# ---------------------------------------------------------------------------

def _drive_to_payment_pending(runner):
    _init(runner)
    _invoke(runner, "flow", "prereq", "check")
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    _invoke(runner, "flow", "buyer", "resolve")
    r = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)
    assert r.exit_code == 0, r.stdout
    return _stdout_json(r)


def test_book_emits_price_display(runner, isolated_session):
    body = _drive_to_payment_pending(runner)
    assert body["data"]["price_display"] == "$1.00"
    assert body["data"]["amount_minor_units"] == 100
    assert body["data"]["price_source"] == "ucp_checkout_total"
    merchant_quote = body["data"]["merchant_quote"]
    assert merchant_quote is not None
    assert merchant_quote["total_minor_units"] == 100
    assert merchant_quote["currency"] == "USD"


def test_book_blocks_mandatory_acceptance_pending(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.payment_helpers as payment_helpers
    from booking_agent.ucp_checkout import MandatoryAcceptancePending

    async def _blocked(*_args, **_kwargs):
        raise MandatoryAcceptancePending(
            "Merchant requires acceptance before payment",
            continue_url="https://merchant.test/accept",
            links=[{"rel": "terms", "href": "https://merchant.test/terms"}],
        )

    monkeypatch.setattr(
        payment_helpers,
        "_create_booking_via_ucp_checkout",
        _blocked,
    )

    _init(runner)
    _invoke(runner, "flow", "prereq", "check")
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    _invoke(runner, "flow", "buyer", "resolve")

    r = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)
    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "mandatory_acceptance_pending"
    assert body["data"]["continue_url"] == "https://merchant.test/accept"
    assert body["data"]["links"][0]["href"] == "https://merchant.test/terms"

    r_pay = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        "x" * 100,
    )
    assert r_pay.exit_code == 1, r_pay.stdout
    assert _stdout_json(r_pay)["data"]["error"]["code"] != "approval_not_surfaced_to_user"


# ---------------------------------------------------------------------------
# Fast booking wait (book poll --wait)
# ---------------------------------------------------------------------------

def test_book_poll_wait_confirmed(runner, isolated_session):
    """`book poll --wait` returns CONFIRMED in one invocation once the booking is paid."""
    from support.mock import mark_booking_paid

    book_body = _drive_to_payment_pending(runner)
    booking_id = book_body["data"]["booking_id"]
    mark_booking_paid(booking_id)

    r = _invoke(
        runner, "flow", "book", "poll",
        "--wait", "--poll-interval", "0.05", "--timeout", "5",
    )
    assert r.exit_code == 0, r.stdout
    body = _stdout_json(r)
    assert body["phase"] == "CONFIRMED"
    assert body["data"]["status"] == "confirmed"


def test_book_poll_wait_timeout(runner, isolated_session):
    """`book poll --wait` exits with poll_timeout when the booking never confirms."""
    _drive_to_payment_pending(runner)  # booking created but left unpaid

    r = _invoke(
        runner, "flow", "book", "poll",
        "--wait", "--poll-interval", "0.05", "--timeout", "0.2",
    )
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "poll_timeout"
    assert body["next_command"] == "booking-agent flow book poll --wait"


def test_pay_request_second_call_does_not_reemit_url(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult

    _drive_to_payment_pending(runner)

    def fake_create(**kwargs):
        return SpendRequestCreateResult(
            id="lsrq_once",
            approval_url="https://link.test/approve-once",
            raw={"id": "lsrq_once"},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    context = "x" * 100
    r1 = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
    )
    assert r1.exit_code == 0, r1.stdout + r1.stderr
    assert r1.stdout.splitlines()[0] == "https://link.test/approve-once"

    r2 = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
    )
    assert r2.exit_code == 1, r2.stdout + r2.stderr
    assert not r2.stdout.startswith("https://")
    body = _stdout_json(r2)
    assert body["data"]["error"]["code"] == "approval_already_surfaced"
    assert body["next_command"] == "booking-agent flow pay poll"


# ---------------------------------------------------------------------------
# Business-zone availability and buyer timezone ("show both, ask once")
# ---------------------------------------------------------------------------

_JERUSALEM = (31.7683, 35.2137)
_YUMA = (32.6927, -114.6277)
_BUYER = {"name": "Ada Lovelace", "email": "ada@booking-agent.test"}


def _geocoder(monkeypatch, coords):
    """Enable the buyer-zone lookup with a fake geocoder (None means it is down)."""
    import booking_agent_cli.timezone_context as tc

    monkeypatch.setenv(tc.LOOKUP_ENV, "on")

    def fake(_params):
        if coords is None:
            raise tc.BuyerTimezoneError("geocoder_unavailable")
        return [{"lat": str(coords[0]), "lon": str(coords[1])}]

    import booking_agent_cli.geocode as geo

    monkeypatch.setattr(geo, "_geocode_http", fake)


def _link_address(monkeypatch, *, with_address=True):
    from support.mock_link_cli import YUMA_SHIPPING_ADDRESS

    mock_link_cli_user_info(
        monkeypatch, _BUYER,
        shipping_addresses=[YUMA_SHIPPING_ADDRESS] if with_address else [],
    )


def _to_slots_list(runner, *args):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _invoke(runner, "flow", "calendar", "skip")
    avail = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID, *args)
    assert avail.exit_code == 0, avail.stdout + avail.stderr
    return _stdout_json(avail), _stdout_json(_invoke(runner, "flow", "slots", "list"))


def _select_first_and_resolve(runner, slots_body):
    slot_id = slots_body["data"]["slots"][0]["slot_id"]
    r = _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    assert r.exit_code == 0, r.stdout
    _satisfy_link_auth_gate()
    r = _invoke(runner, "flow", "buyer", "resolve")
    assert r.exit_code == 0, r.stdout
    return _stdout_json(r)


def test_availability_windows_and_query_use_business_timezone(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    backend = flow.resolve_backend()
    seen = {}
    original = backend.query_availability

    async def tracked(profile, sid, start_date, end_date, timezone=None):
        seen.update(start=start_date, end=end_date, timezone=timezone)
        return await original(profile, sid, start_date, end_date, timezone)

    monkeypatch.setattr(backend, "query_availability", tracked)
    avail, _ = _to_slots_list(runner, "--target-date", "2026-11-02")
    assert seen == {
        "start": "2026-11-02T00:00:00-07:00",
        "end": "2026-11-02T23:59:59-07:00",
        "timezone": "America/Phoenix",
    }
    assert avail["data"]["business_timezone"] == "America/Phoenix"


def test_default_window_starts_tomorrow_in_business_zone(runner, isolated_session, monkeypatch):
    from booking_agent_cli.session import must_load

    _install_ucp_business(
        monkeypatch, {"name": "Zoned", "currency": "USD", "timezone": "Asia/Jerusalem"},
    )
    _init(runner)
    assert _business_use(runner).exit_code == 0
    _invoke(runner, "flow", "use-ucp-checkout-mode")
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    window = must_load().availability_window
    assert window.from_.endswith(("+02:00", "+03:00"))
    assert "T00:00:00" in window.from_ and "T23:59:59" in window.to


def test_slots_list_shows_both_zones_and_one_notice_when_buyer_differs(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import must_load

    _link_address(monkeypatch)
    _geocoder(monkeypatch, _JERUSALEM)
    avail, body = _to_slots_list(runner)

    assert avail["data"]["buyer_timezone"] == "Asia/Jerusalem"
    data = body["data"]
    assert data["business_timezone"] == "America/Phoenix"
    assert data["buyer_timezone"] == "Asia/Jerusalem"
    assert data["timezone_notice"].startswith("I'm detecting a timezone difference")
    assert "Phoenix time (America/Phoenix)" in data["timezone_notice"]
    assert "Jerusalem time (Asia/Jerusalem)" in data["timezone_notice"]
    for slot in data["slots"]:
        assert slot["business_start_display"].endswith("(America/Phoenix)")
        assert slot["buyer_start_display"].endswith("(Asia/Jerusalem)")
        assert slot["start"] and slot["slot_id"]
    assert "timezone_notice" in body["prompt"] and "parentheses" in body["prompt"]
    session = must_load()
    assert session.buyer_timezone_source == "link_shipping_address"
    assert session.slots_presented_dual_zone is True


def test_same_zone_buyer_gets_business_times_without_notice(runner, isolated_session, monkeypatch):
    _link_address(monkeypatch)
    _geocoder(monkeypatch, _YUMA)
    _, body = _to_slots_list(runner)
    data = body["data"]
    assert "timezone_notice" not in data
    assert "buyer_start_display" not in data["slots"][0]
    assert data["slots"][0]["business_start_display"].endswith("(America/Phoenix)")

    recap = _select_first_and_resolve(runner, body)
    assert "buyer_display" not in recap["data"]["selected_slot"]
    assert "timezone_notice" not in recap["data"]


def test_dual_list_pick_carries_both_times_into_the_single_confirmation(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.commands.flow import _skill_compliance_failures
    from booking_agent_cli.session import must_load

    _link_address(monkeypatch)
    _geocoder(monkeypatch, _JERUSALEM)
    _, body = _to_slots_list(runner)
    recap = _select_first_and_resolve(runner, body)

    session = must_load()
    assert session.timezone_difference_shown is True
    selected = recap["data"]["selected_slot"]
    assert selected["business_display"].endswith("(America/Phoenix)")
    assert selected["buyer_display"].endswith("(Asia/Jerusalem)")
    assert "timezone_notice" not in recap["data"]
    assert "buyer_display in parentheses" in recap["prompt"]
    assert recap["next_command"] == "booking-agent flow book"
    assert not any("timezone" in f for f in _skill_compliance_failures(session))


def test_geocoder_failure_at_availability_retries_at_buyer_resolve(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import must_load

    _link_address(monkeypatch)
    _geocoder(monkeypatch, None)
    avail, body = _to_slots_list(runner)
    assert avail["data"]["buyer_timezone_unresolved"] == "geocoder_unavailable"
    assert "timezone_notice" not in body["data"]
    assert body["data"]["buyer_timezone_unresolved"] == "geocoder_unavailable"
    assert "(America/Phoenix)" in body["data"]["timezone_fallback_note"]

    _geocoder(monkeypatch, _JERUSALEM)
    recap = _select_first_and_resolve(runner, body)
    assert recap["data"]["timezone_notice"].startswith("I'm detecting a timezone difference")
    assert recap["data"]["selected_slot"]["buyer_display"].endswith("(Asia/Jerusalem)")
    assert "Open with data.timezone_notice" in recap["prompt"]
    session = must_load()
    assert session.buyer_timezone == "Asia/Jerusalem"
    assert session.timezone_difference_shown is True


def test_unresolved_buyer_zone_never_falls_back_to_device_timezone(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import must_load

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    _link_address(monkeypatch, with_address=False)
    _geocoder(monkeypatch, _JERUSALEM)
    _, body = _to_slots_list(runner)
    assert body["data"]["buyer_timezone_unresolved"] == "no_default_address"
    recap = _select_first_and_resolve(runner, body)
    assert recap["data"]["buyer_timezone_unresolved"] == "no_default_address"
    assert "buyer_display" not in recap["data"]["selected_slot"]
    assert "timezone_notice" not in recap["data"]
    session = must_load()
    assert session.buyer_timezone is None
    assert session.buyer_timezone_source is None


def _to_slots_list_in_zone(runner, monkeypatch, zone):
    from booking_agent_cli.session import load_store, save_store

    _install_ucp_business(monkeypatch, {"name": "Zoned", "currency": "USD", "timezone": zone})
    _init(runner)
    assert _business_use(runner).exit_code == 0
    store = load_store()
    store.sessions[0].discover_services = [{
        "service_id": _SERVICE_ID,
        "registry_search_result": {
            "serviceId": _SERVICE_ID,
            "location": {"coordinates": {"lat": 32.6927, "lng": -114.6277}},
        },
    }]
    save_store(store)
    _invoke(runner, "flow", "use-ucp-checkout-mode")
    _invoke(runner, "flow", "calendar", "skip")
    assert _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID).exit_code == 0
    return _stdout_json(_invoke(runner, "flow", "slots", "list"))


def test_business_zone_far_from_service_location_needs_time_confirmation(
    runner, isolated_session, monkeypatch,
):
    """A Yuma service published in Asia/Jerusalem must not be booked on an unverified time."""
    from booking_agent_cli.session import must_load

    body = _to_slots_list_in_zone(runner, monkeypatch, "Asia/Jerusalem")
    conflict = body["data"]["timezone_conflict"]
    assert conflict["business_timezone"] == "Asia/Jerusalem"
    assert conflict["service_location_timezone"] == "America/Phoenix"
    assert "--timezone-confirmed" in body["next_command"]
    assert "ask them to confirm that exact time" in body["prompt"]

    slot_id = body["data"]["slots"][0]["slot_id"]
    refused = _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    assert refused.exit_code == 1
    err = _stdout_json(refused)
    assert err["data"]["error"]["code"] == "timezone_confirmation_required"
    assert "--timezone-confirmed" in err["next_command"]
    assert must_load().slot_id is None

    ok = _invoke(
        runner, "flow", "slots", "select", "--slot-id", slot_id,
        "--user-confirmed", "--timezone-confirmed",
    )
    assert ok.exit_code == 0, ok.stdout
    session = must_load()
    assert session.slot_id == slot_id
    assert session.timezone_conflict_confirmed is True


def test_matching_business_zone_needs_no_time_confirmation(runner, isolated_session, monkeypatch):
    body = _to_slots_list_in_zone(runner, monkeypatch, "America/Phoenix")
    assert "timezone_conflict" not in body["data"]
    assert "--timezone-confirmed" not in body["next_command"]


def test_identical_resource_slots_are_listed_once(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.session import must_load

    backend = flow.resolve_backend()
    original = backend.query_availability

    async def doubled(profile, sid, start_date, end_date, timezone=None):
        slots = await original(profile, sid, start_date, end_date, timezone)
        return slots + [s.model_copy(update={"id": f"{s.id}-tech2"}) for s in slots]

    monkeypatch.setattr(backend, "query_availability", doubled)
    avail, body = _to_slots_list(runner)
    slots = body["data"]["slots"]
    assert avail["data"]["resource_slot_count"] == 2 * avail["data"]["slot_count"]
    assert len({(s["start"], s["end"]) for s in slots}) == len(slots)
    assert all(s["resource_options"] == 2 for s in slots)

    alternate = f"{slots[0]['slot_id']}-tech2"
    r = _invoke(runner, "flow", "slots", "select", "--slot-id", alternate, "--user-confirmed")
    assert r.exit_code == 0, r.stdout
    assert must_load().slot_id == slots[0]["slot_id"]


def test_home_visit_with_unknown_buyer_zone_needs_time_confirmation():
    from booking_agent_cli.commands.flow import _timezone_gate
    from booking_agent_cli.session import Business, Session, SlotRef

    session = Session(
        business=Business(timezone="America/Phoenix"),
        buyer_timezone_error="timezone_lookup_dependency_missing",
        live_service={"channel": {"type": "at_buyer_location"}},
        slots_presented=[SlotRef(slot_id="s1", start="2026-10-06T13:00:00Z", end="2026-10-06T14:00:00Z")],
    )
    assert _timezone_gate(session) == {
        "buyer_timezone_unresolved_for_home_visit": "timezone_lookup_dependency_missing",
    }
    session.live_service = {"channel": {"type": "at_business_location"}}
    assert _timezone_gate(session) == {}
    session.live_service = {"channel": {"type": "at_buyer_location"}}
    session.buyer_timezone = "America/Phoenix"
    assert _timezone_gate(session) == {}


def test_incident_replay_needs_each_user_confirmation_before_a_slot_is_held(
    runner, isolated_session, monkeypatch,
):
    """Replays the AC repair incident: a shop-only service shown as a less-direct match,
    a business publishing Asia/Jerusalem for a Yuma location, and a duplicate resource slot.
    Each mismatch must stop for the user instead of passing silently."""
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.session import load_session, load_store, save_store

    backend = flow.resolve_backend()
    original = backend.query_availability

    async def doubled(profile, sid, start_date, end_date, timezone=None):
        slots = await original(profile, sid, start_date, end_date, timezone)
        return slots + [s.model_copy(update={"id": f"{s.id}-tech2"}) for s in slots]

    monkeypatch.setattr(backend, "query_availability", doubled)
    _install_ucp_business(
        monkeypatch, {"name": "HVAC-Koby", "currency": "USD", "timezone": "Asia/Jerusalem"},
    )
    _init(runner)
    store = load_store()
    store.sessions[0].discover_services = [{
        "service_id": _SERVICE_ID,
        "service_name": "AC Repair",
        "price_display": "$1.00",
        "channel": {"type": "at_business_location"},
        "registry_search_result": {
            "serviceId": _SERVICE_ID,
            "location": {"coordinates": {"lat": 32.6927, "lng": -114.6277}},
        },
    }]
    save_store(store)

    label = "at the business location, not a home visit"
    use = ["flow", "business", "use", "--profile-url", _PROFILE_URL,
           "--service-id", _SERVICE_ID, "--less-direct", label]
    gate = _stdout_json(_invoke(runner, *use))
    assert gate["awaiting_user"] == "intent_mismatch_confirmation"
    assert load_session().service_id is None
    r = _invoke(runner, *use, "--mismatch-confirmed")
    assert r.exit_code == 0, r.stdout
    _invoke(runner, "flow", "use-ucp-checkout-mode")
    _invoke(runner, "flow", "calendar", "skip")
    assert _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID).exit_code == 0

    body = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slots = body["data"]["slots"]
    assert len({(s["start"], s["end"]) for s in slots}) == len(slots)
    assert body["data"]["timezone_conflict"]["service_location_timezone"] == "America/Phoenix"

    pick = slots[0]["slot_id"]
    refused = _invoke(runner, "flow", "slots", "select", "--slot-id", pick, "--user-confirmed")
    assert _stdout_json(refused)["data"]["error"]["code"] == "timezone_confirmation_required"
    ok = _invoke(
        runner, "flow", "slots", "select", "--slot-id", f"{pick}-tech2",
        "--user-confirmed", "--timezone-confirmed",
    )
    assert ok.exit_code == 0, ok.stdout
    session = load_session()
    assert session.slot_id == pick
    assert session.selection_mismatch == label
    assert session.timezone_conflict_confirmed is True


def test_deposit_terms_are_localized_to_business_time():
    from booking_agent_cli.commands.flow import _localized_messages, _localized_schedules
    from booking_agent_cli.session import Business, Session

    session = Session(business=Business(timezone="Asia/Jerusalem"))
    schedules = _localized_schedules(
        session, [{"amount": 100, "due_at": "2026-10-05T13:00:00Z"}], "USD",
    )
    assert schedules[0]["amount_display"] == "$1.00"
    assert schedules[0]["due_at_display"] == "Mon, Oct 5, 2026, 4:00 PM IDT (Asia/Jerusalem)"
    assert schedules[0]["due_at"] == "2026-10-05T13:00:00Z"
    messages = _localized_messages(
        session, [{"type": "policy", "content": "Refundable until 2026-10-05T13:00:00Z."}],
    )
    assert messages[0]["content_display"] == (
        "Refundable until Mon, Oct 5, 2026, 4:00 PM IDT (Asia/Jerusalem)."
    )


def test_compliance_fails_when_zones_differ_and_dual_list_never_shown():
    from booking_agent_cli.commands.flow import _skill_compliance_failures
    from booking_agent_cli.session import Business, Session, SlotRef

    slot = SlotRef(slot_id="s1", start="2026-10-06T14:00:00+00:00", end="2026-10-06T15:00:00+00:00")
    session = Session(
        business=Business(timezone="America/Phoenix"),
        buyer_timezone="Asia/Jerusalem",
        slots_presented=[slot],
        slot_id="s1",
    )
    assert any("timezone_difference_shown=False" in f for f in _skill_compliance_failures(session))
    session.timezone_difference_shown = True
    assert not any("timezone" in f for f in _skill_compliance_failures(session))
    session.timezone_difference_shown = False
    session.buyer_timezone = "America/Phoenix"
    assert not any("timezone" in f for f in _skill_compliance_failures(session))


# ---------------------------------------------------------------------------
# Route-aware booking confirmation and narration
# ---------------------------------------------------------------------------


def _recap_for(route, *, mismatch=False):
    from booking_agent_cli.commands.flow import _buyer_recap
    from booking_agent_cli.session import Session

    price = {
        "price_display": "$0.00" if route == "free" else "$40.00",
        "pricing_consistency": (
            {"mismatch": True, "registry_price_display": "$30.00",
             "live_catalog_price_display": "$40.00"}
            if mismatch else {}
        ),
    }
    return _buyer_recap(Session(payment_route=route), price, dual=False, notice_needed=False)


@pytest.mark.parametrize("route", ["free", "at_service"])
def test_direct_route_recap_never_mentions_charging_the_wallet(route):
    prompt, summary = _recap_for(route)
    lowered = prompt.lower()
    assert "wallet" not in lowered
    assert "charge" not in lowered
    assert "combined confirmation" in lowered
    assert summary["route"] == route
    assert summary["charge_now_display"] is None


def test_free_recap_says_free_and_at_service_says_paid_at_appointment():
    free_prompt, _ = _recap_for("free")
    assert "this service is free" in free_prompt
    at_service_prompt, summary = _recap_for("at_service")
    assert "at the appointment" in at_service_prompt
    assert summary["due_at_appointment_display"] == "$40.00"


def test_at_booking_recap_asks_to_book_and_charge_the_exact_total():
    prompt, summary = _recap_for("at_booking")
    assert "charge that total to their Link wallet" in prompt
    assert summary["charge_now_display"] == "$40.00"


def test_deposit_recap_charges_only_the_deposit_due_now():
    prompt, summary = _recap_for("deposit_required")
    assert "charge only the deposit due now" in prompt
    assert "shown before anything is charged" in prompt
    assert summary["total_display"] == "$40.00"


def test_recap_keeps_live_catalog_price_on_mismatch_for_every_route():
    for route in ("free", "at_service", "at_booking", "deposit_required"):
        prompt, _ = _recap_for(route, mismatch=True)
        assert "live catalog" in prompt and "'$30.00'" in prompt


def _set_mock_free_service(monkeypatch):
    import booking_agent_cli.commands.flow as flow

    backend = flow.resolve_backend()
    original_get_service = backend.get_service

    async def free_service(profile, service_id):
        service = await original_get_service(profile, service_id)
        policies = service.policies.model_copy(
            update={"requires_payment": False, "payment_timing": None},
        )
        return service.model_copy(update={"policies": policies})

    monkeypatch.setattr(backend, "get_service", free_service)


@pytest.mark.parametrize("route", ["free", "at_service"])
def test_direct_booking_recap_and_narration_skip_wallet_language(
    runner, isolated_session, monkeypatch, route,
):
    if route == "free":
        _set_mock_free_service(monkeypatch)
    else:
        _set_mock_payment_timing(monkeypatch, "at_service")
    _init(runner)
    r = _business_use(runner)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["data"]["payment_route"] == route
    _invoke(runner, "flow", "calendar", "skip")
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    body = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    mock_link_cli_user_info(monkeypatch, _BUYER)
    recap = _select_first_and_resolve(runner, body)
    assert recap["data"]["payment_route"] == route
    assert "wallet" not in recap["prompt"].lower()
    assert "charge" not in recap["prompt"].lower()

    booked = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)
    assert booked.exit_code == 0, booked.stdout + booked.stderr
    out = _stdout_json(booked)
    assert out["next_command"] == "booking-agent flow book poll --wait"
    narration = out["data"]["booking_narration"]
    assert "nothing was charged" in narration.lower()
    assert "waiting for the business to confirm" in narration
    assert "wallet" not in narration.lower() and "wallet" not in out["prompt"].lower()
    assert "data.booking_narration" in out["prompt"]
    if route == "free":
        assert "free" in narration
    else:
        assert "at the appointment" in narration


def test_skill_documents_timezone_and_route_aware_confirmation():
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[3] / "skills" / "booking-agent" / "SKILL.md"
    ).read_text()
    assert "take it from this device, the agent" in text
    assert "data.timezone_notice" in text and "buyer_start_display" in text
    assert "There is no separate timezone question" in text
    for route in ("`free`", "`at_service`", "`at_booking`", "`deposit_required`"):
        assert f"- **{route}:**" in text
    assert "data.booking_narration" in text
    assert "charge **only the deposit due now**" in text
