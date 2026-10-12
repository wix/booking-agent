"""Calendar status, modes, and create-event flow tests."""
import json

from booking_agent_cli.main import app
from booking_agent_cli.session import (
    Business,
    Session,
    SlotRef,
    add_session,
    load_store,
    must_load,
    save_session,
    save_store,
)


def _stdout_json(result):
    dec = json.JSONDecoder()
    s = result.stdout.strip()
    idx = 0
    last = None
    while idx < len(s):
        while idx < len(s) and s[idx].isspace():
            idx += 1
        if idx >= len(s):
            break
        obj, end = dec.raw_decode(s, idx)
        last = obj
        idx = end
    assert last is not None, f"no JSON in stdout: {result.stdout!r}"
    return last


def _seed_session(
    tmp_path,
    monkeypatch,
    *,
    phase: str = "BUSINESS_SELECTED",
    calendar_state: str = "unset",
    calendar_mode: str = "unset",
):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    store = load_store()
    session = Session(
        flow_phase=phase,
        user_request="book a haircut",
        business=Business(profile_url="https://example.com/profile", name="Salon"),
        checkout_prereq_completed=True,
        calendar_gate_completed=calendar_state != "unset",
        calendar_state=calendar_state,
        calendar_mode=calendar_mode,
        service_id="svc-1",
    )
    add_session(store, session)
    save_store(store)
    return session


def test_calendar_skip_sets_mode_none(runner, tmp_path, monkeypatch):
    _seed_session(tmp_path, monkeypatch)
    result = runner.invoke(app, ["flow", "calendar", "skip"])
    assert result.exit_code == 0
    s = must_load()
    assert s.calendar_mode == "none"
    assert s.calendar_state == "skipped"
    store = load_store()
    assert store.last_calendar_state == "skipped"
    assert store.last_calendar_mode == "none"


def test_calendar_ask_auto_applies_remembered_preference(runner, tmp_path, monkeypatch):
    from booking_agent_cli.calendar_preference import save_calendar_preference

    _seed_session(tmp_path, monkeypatch)
    save_calendar_preference(state="connected", mode="events")
    monkeypatch.setenv("GOOGLECALENDAR_ACCESS_TOKEN", "tok-write")

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "scope": (
                    "https://www.googleapis.com/auth/calendar.events "
                    "https://www.googleapis.com/auth/calendar.freebusy"
                ),
                "expires_in": 3600,
            }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            return FakeResp()

    monkeypatch.setattr("booking_agent.calendar.scope.httpx.Client", FakeClient)
    result = runner.invoke(app, ["flow", "calendar", "ask"])
    assert result.exit_code == 0
    body = _stdout_json(result)
    assert body["next_command"] == "booking-agent flow availability"
    assert body["data"]["remembered_preference_applied"] is True
    s = must_load()
    assert s.calendar_gate_completed is True
    assert s.calendar_mode == "events"


def test_calendar_ask_still_prompts_when_not_connected(runner, tmp_path, monkeypatch):
    _seed_session(tmp_path, monkeypatch)
    monkeypatch.delenv("GOOGLECALENDAR_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "operator-client-id")
    result = runner.invoke(app, ["flow", "calendar", "ask"])
    assert result.exit_code == 0
    body = _stdout_json(result)
    assert body["awaiting_user"] == "calendar_consent"
    assert "remembered_preference_applied" not in body.get("data", {})


_SECRET_TOKEN = "ya29.secret-stale-token"


def _tokeninfo_rejects(monkeypatch):
    import httpx

    from booking_agent.calendar import scope

    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "invalid_token"})
        )
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scope.httpx, "Client", _client)


def _assert_no_token_leak(result):
    assert _SECRET_TOKEN not in result.stdout
    assert "tokeninfo?" not in result.stdout
    assert "access_token=" not in result.stdout


def test_calendar_ask_offers_reconnect_for_stale_remembered_mode(
    runner, tmp_path, monkeypatch,
):
    from booking_agent_cli.calendar_event import load_cached_token, write_token_cache
    from booking_agent_cli.calendar_preference import save_calendar_preference

    _seed_session(tmp_path, monkeypatch)
    monkeypatch.delenv("GOOGLECALENDAR_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "operator-client-id")
    save_calendar_preference(state="connected", mode="events")
    write_token_cache("cli", _SECRET_TOKEN)
    _tokeninfo_rejects(monkeypatch)
    result = runner.invoke(app, ["flow", "calendar", "ask"])
    assert result.exit_code == 0
    _assert_no_token_leak(result)
    body = _stdout_json(result)
    assert body["awaiting_user"] == "calendar_reconnect"
    assert body["next_command"] == "booking-agent flow calendar connect --mode events"
    data = body["data"]
    assert data["remembered_calendar_mode"] == "events"
    assert data["connection_problem"]["code"] == "invalid_token"
    assert data["connection_problem"]["cached_token_removed"] is True
    assert [c["command"] for c in data["calendar_reconnect_choices"]] == [
        "booking-agent flow calendar connect --mode events",
        "booking-agent flow calendar skip --session-only",
    ]
    assert "(3)" not in body["prompt"]
    assert load_cached_token("cli") is None
    s = must_load()
    assert s.calendar_gate_completed is False
    assert load_store().last_calendar_state == "connected"


def test_calendar_ask_stale_without_oauth_skips_for_session_only(
    runner, tmp_path, monkeypatch,
):
    from booking_agent_cli.calendar_preference import save_calendar_preference

    _seed_session(tmp_path, monkeypatch)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.setenv("GOOGLECALENDAR_ACCESS_TOKEN", _SECRET_TOKEN)
    save_calendar_preference(state="connected", mode="freebusy")
    _tokeninfo_rejects(monkeypatch)
    result = runner.invoke(app, ["flow", "calendar", "ask"])
    assert result.exit_code == 0
    _assert_no_token_leak(result)
    body = _stdout_json(result)
    assert body.get("awaiting_user") is None
    assert body["next_command"] == "booking-agent flow availability"
    assert body["data"]["calendar_checking_unavailable"] is True
    assert body["data"]["stale_calendar_mode"] == "freebusy"
    assert "unavailable" in body["prompt"]
    s = must_load()
    assert s.calendar_gate_completed is True
    assert s.calendar_state == "skipped"
    store = load_store()
    assert (store.last_calendar_state, store.last_calendar_mode) == ("connected", "freebusy")


def test_calendar_skip_session_only_keeps_saved_preference(runner, tmp_path, monkeypatch):
    from booking_agent_cli.calendar_preference import save_calendar_preference

    _seed_session(tmp_path, monkeypatch)
    save_calendar_preference(state="connected", mode="events")
    result = runner.invoke(app, ["flow", "calendar", "skip", "--session-only"])
    assert result.exit_code == 0
    body = _stdout_json(result)
    assert body["next_command"] == "booking-agent flow availability"
    s = must_load()
    assert s.calendar_gate_completed is True
    assert s.calendar_state == "skipped"
    assert s.calendar_mode == "none"
    store = load_store()
    assert (store.last_calendar_state, store.last_calendar_mode) == ("connected", "events")


def test_calendar_status_never_leaks_rejected_token(runner, tmp_path, monkeypatch):
    _seed_session(tmp_path, monkeypatch)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.setenv("GOOGLECALENDAR_ACCESS_TOKEN", _SECRET_TOKEN)
    _tokeninfo_rejects(monkeypatch)
    result = runner.invoke(app, ["flow", "calendar", "status"])
    assert result.exit_code == 0
    _assert_no_token_leak(result)
    body = _stdout_json(result)
    assert body["data"]["error"] == "invalid_token"
    assert body["data"]["connection_problem"]["token_source"] == "env"


def test_calendar_ask_auto_skips_without_oauth_client(runner, tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLECALENDAR_ACCESS_TOKEN", raising=False)
    _seed_session(tmp_path, monkeypatch)
    store = load_store()
    assert store.last_calendar_state is None
    assert store.last_calendar_mode is None
    result = runner.invoke(app, ["flow", "calendar", "ask"])
    assert result.exit_code == 0
    body = _stdout_json(result)
    assert body["next_command"] == "booking-agent flow availability"
    assert body["data"]["calendar_state"] == "skipped"
    assert body["data"]["calendar_mode"] == "none"
    assert body["data"]["local_oauth_configured"] is False
    s = must_load()
    assert s.calendar_gate_completed is True
    assert s.calendar_state == "skipped"
    store = load_store()
    assert store.last_calendar_state is None
    assert store.last_calendar_mode is None


def test_calendar_connect_oauth_not_configured(runner, tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    _seed_session(tmp_path, monkeypatch)
    result = runner.invoke(app, ["flow", "calendar", "connect", "--mode", "freebusy"])
    assert result.exit_code == 1
    body = _stdout_json(result)
    assert body["data"]["error"]["code"] == "calendar_oauth_not_configured"
    assert body["next_command"] == "booking-agent flow calendar skip"


def test_calendar_status_reports_not_connected(runner, tmp_path, monkeypatch):
    # Drop a client id or token leaked by an earlier flow command. _seed_session
    # then points the session dir and HOME at tmp_path, so the real
    # ~/.booking-agent/.env is not loaded back into the process.
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_REDIRECT_PORT", raising=False)
    monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN_CACHE_DIR", raising=False)
    monkeypatch.delenv("GOOGLECALENDAR_ACCESS_TOKEN", raising=False)
    _seed_session(tmp_path, monkeypatch)
    result = runner.invoke(app, ["flow", "calendar", "status"])
    assert result.exit_code == 0
    body = _stdout_json(result)
    assert body["next_command"] == "booking-agent flow calendar ask"
    assert body["data"]["connected"] is False
    assert body["data"]["local_oauth_configured"] is False


def test_create_event_skipped_for_freebusy_only(runner, tmp_path, monkeypatch):
    _seed_session(
        tmp_path,
        monkeypatch,
        phase="CONFIRMED",
        calendar_state="connected",
        calendar_mode="freebusy",
    )
    result = runner.invoke(app, ["flow", "calendar", "create-event"])
    assert result.exit_code == 0
    body = _stdout_json(result)
    assert body["data"]["skipped"] is True
    assert body["data"]["reason"] == "freebusy_only"


def test_create_event_success(runner, tmp_path, monkeypatch):
    session = _seed_session(
        tmp_path,
        monkeypatch,
        phase="CONFIRMED",
        calendar_state="connected",
        calendar_mode="events",
    )
    session.slot_id = "slot-1"
    session.slots_presented = [
        SlotRef(
            slot_id="slot-1",
            start="2026-07-10T10:00:00Z",
            end="2026-07-10T11:00:00Z",
        )
    ]
    session.live_service = {"title": "Haircut"}
    save_session(session)

    from booking_agent_cli.calendar_event import write_token_cache

    write_token_cache("cli", "tok-write", expires_in=3600)

    class FakeResp:
        status_code = 200

        def json(self):
            return {"id": "evt-123", "htmlLink": "https://calendar.google.com/event"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            return FakeResp()

    monkeypatch.setattr("booking_agent_cli.calendar_event.httpx.Client", FakeClient)
    result = runner.invoke(app, ["flow", "calendar", "create-event"])
    assert result.exit_code == 0
    body = _stdout_json(result)
    assert body["data"]["event_id"] == "evt-123"
    assert body["data"]["html_link"] == "https://calendar.google.com/event"
    assert "raw html_link URL" in body["prompt"]
    assert "https://calendar.google.com/event" in body["prompt"]


def test_event_description_uses_public_business_site_not_profile_document():
    """A person opening the calendar link should land on the business site.

    The profile URL ends in /.well-known/ucp, which is a JSON document, not the
    page the buyer can visit.
    """
    from booking_agent_cli.calendar_event import build_event_body

    session = Session(
        user_request="fix the window unit",
        business=Business(
            profile_url="https://ranya28.wixsite.com/usp-at-service/.well-known/ucp",
            name="USP At Service",
        ),
        slot_id="slot-1",
        slots_presented=[
            SlotRef(slot_id="slot-1", start="2026-10-06T16:30:00Z", end="2026-10-06T17:30:00Z")
        ],
    )
    body, err = build_event_body(session)
    assert err is None
    assert body is not None
    assert "Business: https://ranya28.wixsite.com/usp-at-service" in body["description"].splitlines()

    apex = Session(
        user_request="fix the window unit",
        business=Business(profile_url="https://hvackoby.com/.well-known/ucp", name="HVAC-Koby"),
        slot_id="slot-1",
        slots_presented=[
            SlotRef(slot_id="slot-1", start="2026-10-06T16:30:00Z", end="2026-10-06T17:30:00Z")
        ],
    )
    apex_body, apex_err = build_event_body(apex)
    assert apex_err is None
    assert apex_body is not None
    assert "Business: https://hvackoby.com" in apex_body["description"].splitlines()


_SERVICE_UUID = "03c8286d-7da3-42df-a414-10db326948a8"


def _titled_session(**overrides) -> Session:
    fields = dict(
        user_request="fix the window unit",
        business=Business(profile_url="https://hvackoby.com/.well-known/ucp", name="HVAC-Koby"),
        service_id=_SERVICE_UUID,
        slot_id="slot-1",
        slots_presented=[
            SlotRef(slot_id="slot-1", start="2026-10-06T07:00:00Z", end="2026-10-06T08:00:00Z")
        ],
    )
    fields.update(overrides)
    return Session(**fields)


def test_event_title_uses_live_service_name():
    from booking_agent_cli.calendar_event import build_event_body

    body, err = build_event_body(_titled_session(live_service={"id": _SERVICE_UUID, "name": "Duct Cleaning"}))
    assert err is None
    assert body["summary"] == "HVAC-Koby - Duct Cleaning"


def test_event_title_accepts_legacy_live_service_title():
    from booking_agent_cli.calendar_event import build_event_body

    body, _ = build_event_body(_titled_session(live_service={"title": "Haircut"}))
    assert body["summary"] == "HVAC-Koby - Haircut"


def test_event_title_falls_back_to_discover_service_name():
    from booking_agent_cli.calendar_event import build_event_body

    body, _ = build_event_body(_titled_session(
        live_service={"id": _SERVICE_UUID},
        discover_services=[
            {"service_id": "other", "service_name": "AC Repair"},
            {"service_id": _SERVICE_UUID, "service_name": "Duct Cleaning"},
        ],
    ))
    assert body["summary"] == "HVAC-Koby - Duct Cleaning"


def test_event_title_never_shows_service_id():
    from booking_agent_cli.calendar_event import build_event_body

    body, _ = build_event_body(_titled_session(live_service=None))
    assert body["summary"] == "HVAC-Koby"
    assert _SERVICE_UUID not in body["summary"]


def _described(**overrides) -> str:
    from booking_agent_cli.calendar_event import build_event_body

    body, err = build_event_body(_titled_session(**overrides))
    assert err is None
    assert body is not None
    return body["description"]


def test_event_description_says_the_service_is_free():
    from booking_agent_cli.session import PaymentRef

    text = _described(
        payment_route="free",
        flow_phase="CONFIRMED",
        payment=PaymentRef(status="paid"),
    )
    assert "Payment: This service is free." in text
    assert "manage booking page" not in text
    assert "To cancel or reschedule this booking, use these links:" in text
    assert "Business website: https://hvackoby.com" in text


def test_event_description_says_pay_when_receiving_the_service():
    from booking_agent_cli.session import PaymentRef

    text = _described(
        payment_route="at_service",
        flow_phase="CONFIRMED",
        # Confirmation marks payment status paid even when nothing was collected.
        payment=PaymentRef(status="paid"),
        live_service={
            "name": "Duct Cleaning",
            "pricing": {"amount_minor_units": 4000, "currency": "USD"},
            "channel": {"type": "at_buyer_location"},
        },
        service_address_label="3194 West Rome Street, Yuma AZ 85364",
    )
    assert "Service: Duct Cleaning" in text
    assert "Where: At your address, 3194 West Rome Street, Yuma AZ 85364." in text
    assert "comes to you" not in text
    assert "Payment: Pay $40.00 when you receive the service." in text
    assert "paid in full" not in text
    assert "deposit" not in text.lower()


def test_event_description_says_pay_at_service_without_a_price():
    text = _described(payment_route="at_service", live_service={"name": "Duct Cleaning"})
    assert "Payment: This needs to be paid when you receive the service." in text


def test_event_description_says_deposit_paid_and_how_much_remains():
    from booking_agent_cli.session import BookingRef, Buyer, PaymentRef

    text = _described(
        payment_route="deposit_required",
        flow_phase="CONFIRMED",
        buyer=Buyer(name="Ada Lovelace"),
        payment=PaymentRef(status="paid", order_id="ord_9"),
        due_now_amount_minor=2000,
        ucp_checkout_amount_minor=10000,
        ucp_merchant_quote={"currency": "USD", "total_minor_units": 10000},
        payment_schedules=[
            {"id": "now", "type": "immediate", "amount": 2000},
            {"id": "later", "type": "balance", "amount": 8000},
        ],
        booking=BookingRef(
            id="bk_1",
            booking_url="https://hvackoby.com/bookings/bk_1",
        ),
        live_service={
            "name": "AC Installation Deposit",
            "channel": {"type": "at_buyer_location"},
        },
    )
    assert "Booked for: Ada Lovelace" in text
    assert "Payment: You paid a deposit of $20.00. $80.00 is still due when you receive the service." in text
    assert "Booking ID: bk_1" in text
    assert "Order reference: ord_9" in text
    assert "To cancel or reschedule this booking, open the manage booking page:" in text
    assert "https://hvackoby.com/bookings/bk_1" in text
    assert "use these links" not in text
    assert "paid in full" not in text


def test_event_description_deposit_from_schedules_when_due_now_is_missing():
    from booking_agent_cli.session import PaymentRef

    text = _described(
        payment_route="deposit_required",
        payment=PaymentRef(status="paid"),
        payment_schedules=[
            {"type": "immediate", "amount": 1500},
            {"type": "balance", "amount": 3500},
        ],
        live_service={"name": "Repair", "pricing": {"currency": "USD"}},
    )
    assert "Payment: You paid a deposit of $15.00. $35.00 is still due when you receive the service." in text


def test_event_description_deposit_not_yet_captured():
    text = _described(
        payment_route="deposit_required",
        due_now_amount_minor=2000,
        ucp_checkout_amount_minor=10000,
        ucp_merchant_quote={"currency": "USD"},
        live_service={"name": "Repair"},
    )
    assert "Payment: A deposit of $20.00 is due now, and $80.00 is due when you receive the service." in text


def test_event_description_says_paid_in_full():
    from booking_agent_cli.session import PaymentRef

    text = _described(
        payment_route="at_booking",
        payment=PaymentRef(status="paid"),
        ucp_checkout_amount_minor=10000,
        ucp_merchant_quote={"currency": "USD"},
        live_service={"name": "Duct Cleaning", "channel": {"type": "at_business_location"}},
    )
    assert "Where: At the business." in text
    assert "Payment: You already paid $100.00 for this service in full." in text
    assert "deposit" not in text.lower()


def test_event_description_full_price_still_due_when_not_captured():
    text = _described(
        payment_route="at_booking",
        live_service={
            "name": "Duct Cleaning",
            "pricing": {"amount": 5000, "currency": "EUR"},
        },
    )
    assert "Payment: The full price of €50.00 is due for this booking." in text


def test_event_description_fallback_links_when_no_manage_page():
    from booking_agent_cli.session import BookingRef

    text = _described(
        payment_route="free",
        booking=BookingRef(id="bk_2"),
        ucp_checkout_links=[
            {"type": "terms_of_service", "url": "https://hvackoby.com/terms"},
            {"rel": "privacy_policy", "href": "https://hvackoby.com/privacy"},
            {"type": "terms_of_service", "url": "https://hvackoby.com/terms"},
        ],
    )
    assert "To cancel or reschedule this booking, use these links:" in text
    assert "Business website: https://hvackoby.com" in text
    assert "Terms of service: https://hvackoby.com/terms" in text
    assert "Privacy policy: https://hvackoby.com/privacy" in text
    assert text.count("https://hvackoby.com/terms") == 1
    assert "manage booking page" not in text


def test_event_description_without_any_link_says_to_contact_the_business():
    from booking_agent_cli.session import Business

    text = _described(
        payment_route="free",
        business=Business(name="HVAC-Koby"),
    )
    assert "To cancel or reschedule this booking, contact the business directly." in text
    assert "Business:" not in text


def test_event_description_infers_free_from_the_live_catalog():
    text = _described(
        live_service={
            "name": "Consult",
            "pricing": {"model": "free", "amount": 0, "currency": "USD"},
            "policies": {"requires_payment": False},
        },
    )
    assert "Payment: This service is free." in text


def _fake_google(monkeypatch, *, status_code: int = 204, raises: Exception = None):
    calls = []

    class FakeResp:
        def __init__(self):
            self.status_code = status_code
            self.text = ""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def delete(self, url, headers=None):
            calls.append(url)
            if raises is not None:
                raise raises
            return FakeResp()

    monkeypatch.setattr("booking_agent_cli.calendar_event.httpx.Client", FakeClient)
    return calls


def _session_with_event(tmp_path, monkeypatch, *, token: bool = True) -> Session:
    from booking_agent_cli.calendar_event import record_event_id, write_token_cache

    monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN_CACHE_DIR", raising=False)
    session = _seed_session(
        tmp_path, monkeypatch, phase="CONFIRMED",
        calendar_state="connected", calendar_mode="events",
    )
    record_event_id(session.id, "evt-1", "https://calendar.google.com/event?eid=evt-1")
    if token:
        write_token_cache("cli", "tok-write", expires_in=3600)
    return session


def test_delete_event_removes_it_and_forgets_the_id(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_event import delete_calendar_event, existing_event_id

    session = _session_with_event(tmp_path, monkeypatch)
    calls = _fake_google(monkeypatch, status_code=204)

    result = delete_calendar_event(session)

    assert result == {"ok": True, "status": "deleted", "event_id": "evt-1"}
    assert calls == ["https://www.googleapis.com/calendar/v3/calendars/primary/events/evt-1"]
    assert existing_event_id(session.id) is None


def test_delete_event_treats_an_event_the_user_removed_as_done(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_event import delete_calendar_event, existing_event_id

    session = _session_with_event(tmp_path, monkeypatch)
    _fake_google(monkeypatch, status_code=404)

    result = delete_calendar_event(session)

    assert result["ok"] is True
    assert result["status"] == "already_missing"
    assert existing_event_id(session.id) is None


def test_delete_event_skips_when_no_event_was_created(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_event import delete_calendar_event

    session = _seed_session(tmp_path, monkeypatch, phase="CONFIRMED")
    calls = _fake_google(monkeypatch)

    assert delete_calendar_event(session) == {"ok": True, "status": "skipped", "reason": "no_event"}
    assert calls == []


def test_delete_event_without_a_token_keeps_the_link_for_manual_removal(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_event import delete_calendar_event, existing_event_id

    monkeypatch.delenv("GOOGLECALENDAR_ACCESS_TOKEN", raising=False)
    session = _session_with_event(tmp_path, monkeypatch, token=False)
    calls = _fake_google(monkeypatch)

    result = delete_calendar_event(session)

    assert result["ok"] is False
    assert result["code"] == "calendar_not_connected"
    assert result["html_link"] == "https://calendar.google.com/event?eid=evt-1"
    assert calls == []
    assert existing_event_id(session.id) == "evt-1"


def test_delete_event_api_error_keeps_the_event_id_for_a_retry(tmp_path, monkeypatch):
    from booking_agent_cli.calendar_event import delete_calendar_event, existing_event_id

    session = _session_with_event(tmp_path, monkeypatch)
    _fake_google(monkeypatch, status_code=500)

    result = delete_calendar_event(session)

    assert result["ok"] is False
    assert result["code"] == "calendar_delete_failed"
    assert result["html_link"] == "https://calendar.google.com/event?eid=evt-1"
    assert existing_event_id(session.id) == "evt-1"


def test_delete_event_network_error_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    import httpx

    from booking_agent_cli.calendar_event import delete_calendar_event, existing_event_id

    session = _session_with_event(tmp_path, monkeypatch)
    _fake_google(monkeypatch, raises=httpx.ConnectError("offline"))

    result = delete_calendar_event(session)

    assert result["ok"] is False
    assert result["code"] == "calendar_delete_failed"
    assert existing_event_id(session.id) == "evt-1"


def test_delete_event_command_refuses_a_booking_that_is_still_live(runner, tmp_path, monkeypatch):
    from booking_agent_cli.session import BookingRef

    session = _session_with_event(tmp_path, monkeypatch)
    session.booking = BookingRef(id="b1", status="confirmed")
    save_session(session)
    calls = _fake_google(monkeypatch)

    result = runner.invoke(app, ["flow", "calendar", "delete-event"])

    assert result.exit_code == 1
    assert _stdout_json(result)["data"]["error"]["code"] == "booking_not_canceled"
    assert calls == []


def test_delete_event_command_removes_the_event_of_a_canceled_booking(runner, tmp_path, monkeypatch):
    from booking_agent_cli.session import BookingRef

    session = _session_with_event(tmp_path, monkeypatch)
    session.booking = BookingRef(id="b1", status="canceled")
    save_session(session)
    calls = _fake_google(monkeypatch, status_code=204)

    result = runner.invoke(app, ["flow", "calendar", "delete-event"])

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    assert "evt-1" in json.dumps(_last_json(result.stdout))


def _last_json(text: str) -> dict:
    text = text.strip()
    start = text.rindex("\n{") + 1 if "\n{" in text else 0
    return json.loads(text[start:])


class _FakeCalendarService:
    def __init__(self):
        self.timeouts = []

    async def initiate_connection(self, **_kwargs):
        return {"connected": False, "auth_url": "https://accounts.google.com/o/oauth2/auth?x=1"}

    async def wait_for_auth(self, *, session_id, timeout):
        self.timeouts.append(timeout)
        return {"connected": False, "error": "OAuth timed out."}


def _connect_until_timeout(runner, tmp_path, monkeypatch, *args):
    _seed_session(tmp_path, monkeypatch)
    service = _FakeCalendarService()
    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.calendar_connection_status",
        lambda: {"connected": False, "local_oauth_configured": True},
    )
    monkeypatch.setattr("booking_agent_cli.commands.flow.resolve_calendar_service", lambda: service)
    result = runner.invoke(app, ["flow", "calendar", "connect", "--mode", "events", *args])
    return result, service


def test_calendar_connect_waits_ten_minutes_by_default_and_says_so(runner, tmp_path, monkeypatch):
    result, service = _connect_until_timeout(runner, tmp_path, monkeypatch)

    assert service.timeouts == [600]
    first = json.JSONDecoder().raw_decode(result.stdout.strip())[0]
    assert first["awaiting_user"] == "oauth_approval"
    assert first["data"]["link_valid_minutes"] == 10
    assert "about 10 minutes" in first["prompt"]


def test_calendar_connect_timeout_flag_sets_the_wait(runner, tmp_path, monkeypatch):
    result, service = _connect_until_timeout(runner, tmp_path, monkeypatch, "--timeout", "120")

    assert service.timeouts == [120]
    first = json.JSONDecoder().raw_decode(result.stdout.strip())[0]
    assert first["data"]["link_valid_minutes"] == 2


def test_calendar_connect_timeout_asks_the_user_instead_of_relinking(runner, tmp_path, monkeypatch):
    result, _ = _connect_until_timeout(runner, tmp_path, monkeypatch)

    assert result.exit_code == 1
    body = _last_json(result.stdout)
    assert body["data"]["error"]["code"] == "calendar_oauth_timeout"
    assert body["next_command"] is None
    assert "Would you like a new link" in body["user_message"]
    assert "Would you like a new link" in result.stdout.split("\n{")[-2]
    assert [c["choice"] for c in body["data"]["choices"]] == ["new_link", "continue_without_calendar"]
    assert body["data"]["choices"][1]["command"] == "booking-agent flow calendar skip --session-only"


def test_event_location_and_time_zone_for_a_home_visit():
    from booking_agent_cli.calendar_event import build_event_body

    body, _ = build_event_body(_titled_session(
        business=Business(name="HVAC-Koby", timezone="America/Phoenix"),
        live_service={"name": "AC Repair", "channel": {"type": "at_buyer_location"}},
        service_address_label="3194 West Rome Street, Yuma AZ 85364",
        booking_notes="Window unit, completely unresponsive",
    ))
    assert body["location"] == "3194 West Rome Street, Yuma AZ 85364"
    assert body["start"] == {"dateTime": "2026-10-06T07:00:00Z", "timeZone": "America/Phoenix"}
    assert body["end"]["timeZone"] == "America/Phoenix"
    assert "Your note to the business: Window unit, completely unresponsive" in body["description"]


def test_event_location_uses_the_business_address_when_published():
    from booking_agent_cli.calendar_event import build_event_body

    body, _ = build_event_body(_titled_session(live_service={
        "name": "Haircut",
        "channel": {"type": "at_business_location"},
        "locations": [{"name": "Main salon", "address": "1 Main St, Yuma AZ 85364"}],
    }))
    assert body["location"] == "1 Main St, Yuma AZ 85364"
    assert "Where: At the business, 1 Main St, Yuma AZ 85364." in body["description"]
    assert "timeZone" not in body["start"]


def test_event_location_falls_back_to_the_business_name():
    from booking_agent_cli.calendar_event import build_event_body

    body, _ = build_event_body(_titled_session(
        live_service={"name": "AC Repair", "channel": {"type": "at_buyer_location"}},
    ))
    assert body["location"] == "HVAC-Koby"
    assert "Where: At your address." in body["description"]
