"""``booking-agent flow reschedule`` moves a CONFIRMED booking without rebooking it."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest
from typer.testing import CliRunner

from booking_agent.models.wire import SlotPricing, TimeSlot
from booking_agent.rest_client import BookingServerError, PopAuthorizationError, RevisionMismatch
from booking_agent_cli.engine import recommend_next
from booking_agent_cli.main import app
from booking_agent_cli.session import (
    BookingRef,
    Business,
    PaymentRef,
    Session,
    SlotRef,
    add_session,
    load_session,
    load_store,
    save_store,
)

from support.fixtures import SERVICE_BY_ID

ENDPOINT = "https://biz.example/usp"
SERVICE_ID = "e4d4b7a4-0e66-4104-891b-ba5bb4a04092"  # AC Repair, $1.00
MANAGE_URL = "https://biz.example/manage/b1"
CREDENTIAL = "bsc_live_credential_for_b1_000"


def _at(days: int, hour: int) -> str:
    day = datetime.now(timezone.utc) + timedelta(days=days)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


OLD = SlotRef(slot_id="slot_old", start=_at(2, 10), end=_at(2, 11), duration="PT1H")


def _shown(instant: str) -> str:
    """How a slot time reads when the business named no zone: its own UTC offset."""
    from booking_agent_cli.timezone_context import format_instant

    return format_instant(instant, None)


def _slot(slot_id: str, days: int, hour: int, price: Optional[int] = None) -> TimeSlot:
    return TimeSlot(
        id=slot_id,
        service_id=SERVICE_ID,
        start=_at(days, hour),
        end=_at(days, hour + 1),
        duration="PT1H",
        state="available",
        pricing=SlotPricing(amount=price, currency="USD") if price is not None else None,
    )


def _service(**policy_update: Any):
    svc = SERVICE_BY_ID[SERVICE_ID]
    rescheduling = svc.policies.rescheduling.model_copy(update=policy_update)
    policies = svc.policies.model_copy(update={"rescheduling": rescheduling})
    return svc.model_copy(update={"policies": policies})


def _moved_booking(slot: TimeSlot, *, status: str = "confirmed", revision: str = "r2"):
    return SimpleNamespace(
        id="b1",
        status=SimpleNamespace(value=status),
        slot=SimpleNamespace(id=slot.id, start=slot.start, end=slot.end, duration=slot.duration),
        revision=revision,
        booking_url=None,
    )


class FakeBackend:
    def __init__(self, *, service=None, slots: Optional[List[TimeSlot]] = None,
                 reschedule: Optional[list] = None, fresh: Any = None):
        self.service = service or _service()
        self.slots = slots or []
        self.reschedule_results = list(reschedule or [])
        self.reschedule_calls: List[dict] = []
        self.fresh = fresh

    async def resolve_profile(self, _url):
        return SimpleNamespace(rest_endpoint=ENDPOINT)

    async def get_service(self, _profile, _service_id):
        return self.service

    async def query_availability(self, _profile, _service_id, _start, _end, _tz=None):
        return self.slots

    async def reschedule_booking(self, _profile, _booking_id, slot_id, *, credential=None,
                                 credential_endpoint=None, if_match=None):
        self.reschedule_calls.append({
            "slot_id": slot_id,
            "if_match": if_match,
            "credential": credential.value if credential else None,
            "credential_endpoint": credential_endpoint,
        })
        result = self.reschedule_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def get_booking(self, _profile, _booking_id, **_kwargs):
        return self.fresh


def _confirmed_session(**overrides: Any) -> Session:
    session = Session(
        flow_phase="CONFIRMED",
        business=Business(profile_url="https://biz.example/.well-known/usp", name="Koby's HVAC"),
        service_id=SERVICE_ID,
        live_service={"pricing": {"amount_minor_units": 100}},
        slots_presented=[OLD],
        slot_id=OLD.slot_id,
        slot_user_confirmed=True,
        booking=BookingRef(
            id="b1",
            endpoint=ENDPOINT,
            credential=CREDENTIAL,
            credential_expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            revision="r1",
            booking_url=MANAGE_URL,
        ),
        payment=PaymentRef(status="paid"),
        calendar_state="skipped",
        calendar_mode="none",
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


@pytest.fixture
def seed(isolated_session, monkeypatch):
    def _seed(backend: FakeBackend, session: Optional[Session] = None) -> FakeBackend:
        store = load_store()
        add_session(store, session or _confirmed_session())
        save_store(store)
        monkeypatch.setattr("booking_agent_cli.commands.flow.resolve_backend", lambda: backend)
        return backend
    return _seed


def _invoke(runner: CliRunner, *args: str, exit_code: int = 0) -> dict:
    result = runner.invoke(app, ["flow", *args])
    text = result.stdout or result.output or ""
    assert result.exit_code == exit_code, f"stdout={text!r} exc={result.exception!r}"
    obj, _ = json.JSONDecoder().raw_decode(text.strip())
    return obj


def _to_confirm(runner: CliRunner, slot_id: str = "slot_new") -> dict:
    _invoke(runner, "reschedule", "start")
    _invoke(runner, "reschedule", "availability")
    return _invoke(runner, "reschedule", "select", "--slot-id", slot_id, "--user-confirmed")


def test_same_price_move_keeps_the_booking_confirmed_and_its_credential(runner, seed):
    new = _slot("slot_new", 3, 14)
    backend = seed(FakeBackend(slots=[_slot("slot_old", 2, 10), new], reschedule=[_moved_booking(new)]))

    started = _invoke(runner, "reschedule", "start")
    assert started["next_command"] == "booking-agent flow reschedule availability"
    assert started["data"]["rescheduling_policy"]["allowed"] is True

    listed = _invoke(runner, "reschedule", "availability")
    assert [s["slot_id"] for s in listed["data"]["slots"]] == ["slot_new"]
    assert listed["data"]["slots"][0]["selectable"] is True
    assert listed["data"]["slots"][0]["price_amount"] == 100
    assert listed["awaiting_user"] == "reschedule_slot_choice"

    selected = _invoke(runner, "reschedule", "select", "--slot-id", "slot_new", "--user-confirmed")
    assert selected["awaiting_user"] == "reschedule_confirm"
    assert selected["next_command"] == "booking-agent flow reschedule apply --user-confirmed"
    assert _shown(OLD.start) in selected["prompt"] and _shown(new.start) in selected["prompt"]
    assert "did not name its timezone" in selected["prompt"]
    before_apply = load_session()
    assert before_apply.slot_id == "slot_old"
    assert [s.slot_id for s in before_apply.slots_presented] == ["slot_old"]

    applied = _invoke(runner, "reschedule", "apply", "--user-confirmed")
    assert applied["phase"] == "CONFIRMED"
    assert applied["next_command"] is None
    assert applied["data"]["new_slot"]["slot_id"] == "slot_new"
    assert applied["data"]["previous_slot"]["slot_id"] == "slot_old"
    assert backend.reschedule_calls == [{
        "slot_id": "slot_new",
        "if_match": "r1",
        "credential": CREDENTIAL,
        "credential_endpoint": ENDPOINT,
    }]

    after = load_session()
    assert after.flow_phase == "CONFIRMED"
    assert after.booking.credential == CREDENTIAL
    assert after.booking.revision == "r2"
    assert after.slot_id == "slot_new"
    assert [s.slot_id for s in after.slots_presented] == ["slot_new"]
    assert after.reschedule_original_slot_id is None and not after.reschedule_slots
    assert recommend_next(after) is None

    rows = _invoke(runner, "sessions", "list")["data"]["sessions"]
    assert rows[0]["slot_start"] == new.start
    assert rows[0]["slot_end"] == new.end


def test_confirm_prompt_puts_current_and_new_time_on_labeled_lines(runner, seed):
    new = _slot("slot_new", 3, 14)
    seed(FakeBackend(slots=[new]))

    prompt = _to_confirm(runner)["prompt"]

    lines = prompt.splitlines()
    current = next(line for line in lines if line.startswith("Current time: "))
    moved = next(line for line in lines if line.startswith("New time: "))
    assert _shown(OLD.start) in current and _shown(new.start) not in current
    assert _shown(new.start) in moved and _shown(OLD.start) not in moved
    assert lines.index(moved) == lines.index(current) + 1
    assert "from ... to ..." in prompt


def test_policy_refusal_happens_before_any_slot_is_offered(runner, seed):
    seed(FakeBackend(service=_service(allowed=False)))
    obj = _invoke(runner, "reschedule", "start", exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_not_allowed"
    assert obj["data"]["booking_url"] == MANAGE_URL
    assert load_session().reschedule_original_slot_id is None


def test_positive_reschedule_fee_is_refused(runner, seed):
    seed(FakeBackend(service=_service(fee=500)))
    obj = _invoke(runner, "reschedule", "start", exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_fee_unsupported"


def test_zero_fee_string_from_wix_is_not_a_fee(runner, seed):
    seed(FakeBackend(service=_service(fee="0")))
    _invoke(runner, "reschedule", "start")


def test_a_differently_priced_slot_is_shown_as_unselectable_and_refused(runner, seed):
    seed(FakeBackend(slots=[_slot("slot_peak", 3, 14, price=250)]))
    _invoke(runner, "reschedule", "start")
    listed = _invoke(runner, "reschedule", "availability")
    assert listed["data"]["slots"][0]["selectable"] is False
    assert listed["data"]["slots"][0]["not_selectable_reason"] == "reschedule_price_changed"
    assert listed["awaiting_user"] is None

    obj = _invoke(runner, "reschedule", "select", "--slot-id", "slot_peak", "--user-confirmed",
                  exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_price_changed"
    assert load_session().reschedule_slot_id is None


def test_unknown_price_after_a_captured_charge_is_refused(runner, seed):
    unpriced = _service().model_copy(update={"pricing": None})
    session = _confirmed_session(live_service={}, ucp_checkout_amount_minor=None)
    seed(FakeBackend(service=unpriced, slots=[_slot("slot_new", 3, 14)]), session)
    _invoke(runner, "reschedule", "start")
    _invoke(runner, "reschedule", "availability")
    obj = _invoke(runner, "reschedule", "select", "--slot-id", "slot_new", "--user-confirmed",
                  exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_price_unknown"


def test_selection_and_apply_both_require_user_confirmation(runner, seed):
    seed(FakeBackend(slots=[_slot("slot_new", 3, 14)]))
    _invoke(runner, "reschedule", "start")
    _invoke(runner, "reschedule", "availability")
    obj = _invoke(runner, "reschedule", "select", "--slot-id", "slot_new", exit_code=1)
    assert obj["data"]["error"]["code"] == "slot_not_user_confirmed"

    _invoke(runner, "reschedule", "select", "--slot-id", "slot_new", "--user-confirmed")
    obj = _invoke(runner, "reschedule", "apply", exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_not_user_confirmed"
    assert load_session().slot_id == "slot_old"


def test_status_keeps_pointing_at_the_move_until_it_is_applied_or_aborted(runner, seed):
    seed(FakeBackend(slots=[_slot("slot_new", 3, 14)]))
    assert recommend_next(load_session()) is None
    _to_confirm(runner)

    status = _invoke(runner, "status")
    assert status["awaiting_user"] == "reschedule_confirm"
    assert status["next_command"] == "booking-agent flow reschedule apply --user-confirmed"

    aborted = _invoke(runner, "reschedule", "abort")
    assert aborted["data"]["aborted"] is True
    after = load_session()
    assert after.slot_id == "slot_old" and after.reschedule_slot_id is None
    assert _invoke(runner, "status")["next_command"] is None


def test_revision_mismatch_rereads_and_retries_once(runner, seed):
    new = _slot("slot_new", 3, 14)
    fresh = SimpleNamespace(status=SimpleNamespace(value="confirmed"),
                            slot=SimpleNamespace(id="slot_old"), revision="r9")
    backend = seed(FakeBackend(
        slots=[new],
        reschedule=[RevisionMismatch("stale", sent_revision="r1"), _moved_booking(new, revision="r10")],
        fresh=fresh,
    ))
    _to_confirm(runner)
    _invoke(runner, "reschedule", "apply", "--user-confirmed")
    assert [c["if_match"] for c in backend.reschedule_calls] == ["r1", "r9"]
    assert load_session().booking.revision == "r10"


def test_revision_mismatch_on_a_canceled_booking_reports_it_without_retrying(runner, seed):
    fresh = SimpleNamespace(status=SimpleNamespace(value="canceled"),
                            slot=SimpleNamespace(id="slot_old"), revision="r9")
    backend = seed(FakeBackend(
        slots=[_slot("slot_new", 3, 14)],
        reschedule=[RevisionMismatch("stale", sent_revision="r1")],
        fresh=fresh,
    ))
    _to_confirm(runner)
    obj = _invoke(runner, "reschedule", "apply", "--user-confirmed", exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_booking_not_active"
    assert obj["data"]["status"] == "canceled"
    assert len(backend.reschedule_calls) == 1
    assert load_session().slot_id == "slot_old"


def test_rejected_credential_hands_off_to_the_business_page(runner, seed):
    seed(FakeBackend(
        slots=[_slot("slot_new", 3, 14)],
        reschedule=[PopAuthorizationError("pop_proof_invalid", "nope")],
    ))
    _to_confirm(runner)
    obj = _invoke(runner, "reschedule", "apply", "--user-confirmed", exit_code=1)
    assert obj["data"]["error"]["code"] == "pop_proof_invalid"
    assert obj["data"]["booking_url"] == MANAGE_URL
    after = load_session()
    assert after.slot_id == "slot_old"
    assert after.booking.credential == CREDENTIAL


def test_expired_credential_hands_off_before_offering_slots(runner, seed):
    session = _confirmed_session()
    session.booking.credential_expires_at = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat()
    seed(FakeBackend(), session)
    obj = _invoke(runner, "reschedule", "start", exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_at_business"
    assert obj["data"]["manage_urls"][0] == {"type": "booking", "url": MANAGE_URL}


def test_business_refusal_leaves_the_booking_unchanged(runner, seed):
    seed(FakeBackend(
        slots=[_slot("slot_new", 3, 14)],
        reschedule=[BookingServerError("slot_unavailable", "Slot taken")],
    ))
    _to_confirm(runner)
    obj = _invoke(runner, "reschedule", "apply", "--user-confirmed", exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_failed"
    assert obj["data"]["server_code"] == "slot_unavailable"
    assert load_session().slot_id == "slot_old"


@pytest.mark.parametrize("phase", ["BOOKED", "PAYMENT_PENDING"])
def test_in_progress_bookings_cannot_be_rescheduled(runner, seed, phase):
    seed(FakeBackend(), _confirmed_session(flow_phase=phase))
    obj = _invoke(runner, "reschedule", "start", exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_not_actionable"


def test_a_past_appointment_cannot_be_rescheduled(runner, seed):
    past = SlotRef(slot_id="slot_old", start=_at(-1, 10), end=_at(-1, 11))
    seed(FakeBackend(), _confirmed_session(slots_presented=[past]))
    obj = _invoke(runner, "reschedule", "start", exit_code=1)
    assert obj["data"]["error"]["code"] == "reschedule_not_actionable"


def test_events_mode_points_at_calendar_update_and_a_failed_update_still_reports_the_move(
    runner, seed, monkeypatch,
):
    new = _slot("slot_new", 3, 14)
    seed(
        FakeBackend(slots=[new], reschedule=[_moved_booking(new)]),
        _confirmed_session(calendar_state="connected", calendar_mode="events"),
    )
    _to_confirm(runner)
    applied = _invoke(runner, "reschedule", "apply", "--user-confirmed")
    assert applied["next_command"] == "booking-agent flow calendar update-event"

    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.update_calendar_event",
        lambda _session, user_key: {
            "ok": False, "code": "calendar_write_failed", "message": "Google 500",
        },
    )
    obj = _invoke(runner, "calendar", "update-event", exit_code=1)
    assert obj["data"]["error"]["code"] == "calendar_write_failed"
    assert "Google 500" in obj["data"]["error"]["message"]
    assert load_session().slot_id == "slot_new"


class _FakeResponse:
    def __init__(self, status_code: int, payload: Optional[dict] = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, responses: dict, calls: list):
        self._responses = responses
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def patch(self, url, json, headers):
        self._calls.append(("PATCH", url, json))
        return self._responses["PATCH"]

    def post(self, url, json, headers):
        self._calls.append(("POST", url, json))
        return self._responses["POST"]


@pytest.fixture
def calendar_env(tmp_path, monkeypatch):
    import booking_agent_cli.calendar_event as calendar_event

    monkeypatch.setattr(calendar_event, "_sidecar_path", lambda: str(tmp_path / "events.json"))
    monkeypatch.setattr(calendar_event, "load_cached_token", lambda _key: {"access_token": "tok"})
    calls: list = []

    def install(responses: dict):
        monkeypatch.setattr(
            calendar_event.httpx, "Client", lambda **_kwargs: _FakeClient(responses, calls),
        )
    return calendar_event, calls, install


def _calendar_session() -> Session:
    new = SlotRef(slot_id="slot_new", start=_at(3, 14), end=_at(3, 15))
    return _confirmed_session(slots_presented=[new], slot_id="slot_new")


def test_update_event_patches_the_recorded_event_to_the_new_time(calendar_env):
    calendar_event, calls, install = calendar_env
    session = _calendar_session()
    calendar_event.record_event_id(session.id, "evt_1", "https://calendar/old")
    install({"PATCH": _FakeResponse(200, {"id": "evt_1", "htmlLink": "https://calendar/evt_1"})})

    result = calendar_event.update_calendar_event(session)

    assert result == {"ok": True, "event_id": "evt_1",
                      "html_link": "https://calendar/evt_1", "created": False}
    method, url, body = calls[0]
    assert method == "PATCH" and url.endswith("/evt_1")
    assert body["start"]["dateTime"] == session.slots_presented[0].start


def test_update_event_recreates_a_deleted_event(calendar_env):
    calendar_event, calls, install = calendar_env
    session = _calendar_session()
    calendar_event.record_event_id(session.id, "evt_gone")
    install({
        "PATCH": _FakeResponse(404),
        "POST": _FakeResponse(200, {"id": "evt_2", "htmlLink": "https://calendar/evt_2"}),
    })

    result = calendar_event.update_calendar_event(session)

    assert result["ok"] is True and result["created"] is True
    assert result["event_id"] == "evt_2"
    assert [c[0] for c in calls] == ["PATCH", "POST"]
    assert calendar_event.existing_event_id(session.id) == "evt_2"


def _zoned_session(buyer_tz: Optional[str]) -> Session:
    session = _confirmed_session(buyer_timezone=buyer_tz)
    session.business.timezone = "Asia/Jerusalem"
    return session


def test_reschedule_shows_both_zones_when_buyer_zone_differs(runner, seed):
    new = _slot("slot_new", 3, 14)
    seed(FakeBackend(slots=[new], reschedule=[_moved_booking(new)]),
         _zoned_session("America/Phoenix"))
    _invoke(runner, "reschedule", "start")

    listed = _invoke(runner, "reschedule", "availability")
    data = listed["data"]
    assert data["business_timezone"] == "Asia/Jerusalem"
    assert data["timezone_notice"].startswith("I'm detecting a timezone difference")
    row = data["slots"][0]
    assert row["business_start_display"].endswith("(Asia/Jerusalem)")
    assert row["buyer_start_display"].endswith("(America/Phoenix)")
    assert data["current_slot"]["buyer_start_display"].endswith("(America/Phoenix)")
    assert "parentheses" in listed["prompt"]

    selected = _invoke(runner, "reschedule", "select", "--slot-id", "slot_new", "--user-confirmed")
    assert "(Asia/Jerusalem)" in selected["prompt"] and "(America/Phoenix)" in selected["prompt"]
    assert selected["data"]["new_slot"]["buyer_start_display"].endswith("(America/Phoenix)")

    applied = _invoke(runner, "reschedule", "apply", "--user-confirmed")
    assert "(Asia/Jerusalem)" in applied["prompt"] and "(America/Phoenix)" in applied["prompt"]
    assert applied["data"]["previous_slot"]["buyer_start_display"].endswith("(America/Phoenix)")


def test_reschedule_same_zone_shows_business_times_only(runner, seed):
    seed(FakeBackend(slots=[_slot("slot_new", 3, 14)]), _zoned_session("Asia/Jerusalem"))
    _invoke(runner, "reschedule", "start")
    listed = _invoke(runner, "reschedule", "availability")
    assert "timezone_notice" not in listed["data"]
    assert "buyer_start_display" not in listed["data"]["slots"][0]
    assert listed["data"]["slots"][0]["business_start_display"].endswith("(Asia/Jerusalem)")
    selected = _invoke(runner, "reschedule", "select", "--slot-id", "slot_new", "--user-confirmed")
    assert "(Asia/Jerusalem)" in selected["prompt"]
    assert "parentheses" not in selected["prompt"]


def test_reschedule_window_uses_business_timezone(runner, seed):
    backend = seed(FakeBackend(slots=[]), _zoned_session(None))
    calls = []

    async def tracked(_profile, _service_id, start, end, tz=None):
        calls.append((start, end, tz))
        return []

    backend.query_availability = tracked
    _invoke(runner, "reschedule", "start")
    _invoke(runner, "reschedule", "availability", "--target-date", "2026-11-02")
    assert calls[0] == ("2026-11-02T00:00:00+02:00", "2026-11-02T23:59:59+02:00", "Asia/Jerusalem")
    # The empty single-day retry widens the window in the same business zone.
    assert calls[1][1] == "2026-11-08T23:59:59+02:00"


def test_reschedule_window_without_business_zone_sends_plain_dates(runner, seed):
    backend = seed(FakeBackend(slots=[]))
    calls = []

    async def tracked(_profile, _service_id, start, end, tz=None):
        calls.append((start, end, tz))
        return []

    backend.query_availability = tracked
    _invoke(runner, "reschedule", "start")
    _invoke(runner, "reschedule", "availability", "--target-date", "2026-11-02")
    assert calls[0] == ("2026-11-02", "2026-11-02", None)
    assert calls[1] == ("2026-11-02", "2026-11-08", None)


def test_calendar_window_widens_plain_dates_to_every_offset():
    from booking_agent_cli.commands.flow import _calendar_window

    assert _calendar_window("2026-11-02", "2026-11-08") == (
        "2026-11-01T10:00:00+00:00", "2026-11-09T12:00:00+00:00",
    )
    exact = ("2026-11-02T00:00:00+02:00", "2026-11-02T23:59:59+02:00")
    assert _calendar_window(*exact) == exact
