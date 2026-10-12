"""Session retention: pin actionable bookings, archive after slot end."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from booking_agent_cli.main import app
from booking_agent_cli.session import (
    BookingRef,
    Business,
    Buyer,
    PaymentRef,
    Session,
    SlotRef,
    add_session,
    archive_expired_session,
    is_actionable,
    load_store,
    save_store,
)


def _future_slot() -> tuple[str, str]:
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    end = start + timedelta(hours=1)
    return start.isoformat(), end.isoformat()


def _past_slot() -> tuple[str, str]:
    end = datetime.now(timezone.utc) - timedelta(hours=1)
    start = end - timedelta(hours=1)
    return start.isoformat(), end.isoformat()


def test_is_actionable_for_confirmed_upcoming_booking():
    start, end = _future_slot()
    session = Session(
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start, end=end)],
        booking=BookingRef(id="bk_1"),
    )
    assert is_actionable(session)


@pytest.mark.parametrize("status", ["completed", "canceled", "no_show"])
def test_terminal_booking_status_is_not_actionable(status):
    start, end = _future_slot()
    session = Session(
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start, end=end)],
        booking=BookingRef(id="bk_1", status=status),
    )
    assert not is_actionable(session)


def test_confirmed_booking_status_stays_actionable():
    start, end = _future_slot()
    session = Session(
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start, end=end)],
        booking=BookingRef(id="bk_1", status="confirmed"),
    )
    assert is_actionable(session)


def test_timezone_and_status_fields_round_trip(isolated_session):
    store = load_store()
    session = Session(
        business=Business(name="Biz", timezone="Asia/Jerusalem"),
        service_timezone="Asia/Jerusalem",
        buyer_timezone="America/Phoenix",
        buyer_timezone_source="link_shipping_address",
        timezone_difference_shown=True,
        booking=BookingRef(id="bk_rt", status="canceled"),
    )
    add_session(store, session)
    save_store(store)
    row = next(s for s in load_store().sessions if s.id == session.id)
    assert row.business.timezone == "Asia/Jerusalem"
    assert row.service_timezone == "Asia/Jerusalem"
    assert row.buyer_timezone == "America/Phoenix"
    assert row.buyer_timezone_source == "link_shipping_address"
    assert row.timezone_difference_shown is True
    assert row.booking.status == "canceled"


def test_legacy_session_without_timezone_fields_loads_defaults(isolated_session):
    from booking_agent_cli.session import session_path

    session_path().write_text(json.dumps({
        "schema_version": 2,
        "active_session_id": "legacy",
        "sessions": [{
            "id": "legacy",
            "business": {"profile_url": "https://biz.example/usp", "name": "Old"},
            "booking": {"id": "bk_old"},
        }],
    }))
    row = load_store().sessions[0]
    assert row.business.timezone is None
    assert row.service_timezone is None
    assert row.buyer_timezone is None
    assert row.buyer_timezone_source is None
    assert row.timezone_difference_shown is False
    assert row.booking.status is None


def test_apply_booking_status_records_status():
    from types import SimpleNamespace

    from booking_agent_cli.booking_poll import apply_booking_status

    session = Session(flow_phase="BOOKED", booking=BookingRef(id="bk_1"))
    apply_booking_status(session, SimpleNamespace(status="confirmed", revision=None))
    assert session.booking.status == "confirmed"
    apply_booking_status(session, SimpleNamespace(status="pending", revision=None))
    assert session.booking.status == "pending"


def test_archive_strips_secrets_after_slot_end():
    start, end = _past_slot()
    session = Session(
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start, end=end)],
        booking=BookingRef(id="bk_1", credential="bsc_secret"),
        buyer=Buyer(name="Ada", email="ada@test.com"),
        payment=PaymentRef(spend_request_id="sr_1"),
    )
    assert archive_expired_session(session)
    assert session.booking.id == "bk_1"
    assert session.booking.credential is None
    assert session.buyer.email == ""
    assert session.payment.spend_request_id == "sr_1"
    assert session.payment.spend_approval_surfaced is False
    assert session.payment.spend_approval_user_surfaced is False


def test_pin_actionable_session_over_fifo_cap(runner, isolated_session):
    store = load_store()
    store.max_sessions = 3
    start, end = _future_slot()
    pinned = Session(
        user_request="upcoming haircut",
        flow_phase="CONFIRMED",
        slot_id="slot-pinned",
        slots_presented=[SlotRef(slot_id="slot-pinned", start=start, end=end)],
        booking=BookingRef(id="bk_pinned", credential="bsc_keep"),
        business=Business(name="Salon"),
    )
    add_session(store, pinned)
    save_store(store)

    for i in range(4):
        r = runner.invoke(
            app,
            [
                "flow", "init",
                "--user-request", f"discovery {i}",
                "--force",
            ],
        )
        assert r.exit_code == 0, r.stdout

    store = load_store()
    assert any(s.booking.id == "bk_pinned" for s in store.sessions)
    assert len(store.sessions) >= 3


def test_sessions_list_includes_booking_metadata(runner, isolated_session):
    store = load_store()
    start, end = _future_slot()
    session = Session(
        user_request="cancel me",
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start, end=end)],
        booking=BookingRef(id="bk_list"),
        business=Business(name="Biz"),
    )
    add_session(store, session)
    save_store(store)

    r = runner.invoke(app, ["flow", "sessions", "list"])
    assert r.exit_code == 0
    body = json.loads(r.stdout.strip())
    row = next(x for x in body["data"]["sessions"] if x["booking_id"] == "bk_list")
    assert row["actionable"] is True
    assert row["slot_end"] == end
    assert row["business_name"] == "Biz"
    assert row["booking_status"] is None


def test_sessions_list_canceled_booking_is_not_actionable(runner, isolated_session):
    store = load_store()
    start, end = _future_slot()
    session = Session(
        user_request="already canceled",
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start, end=end)],
        booking=BookingRef(id="bk_canceled", status="canceled"),
        business=Business(name="Biz"),
    )
    add_session(store, session)
    save_store(store)

    r = runner.invoke(app, ["flow", "sessions", "list"])
    assert r.exit_code == 0
    body = json.loads(r.stdout.strip())
    row = next(x for x in body["data"]["sessions"] if x["booking_id"] == "bk_canceled")
    assert row["booking_status"] == "canceled"
    assert row["actionable"] is False


_AC_REQUEST = (
    "My home air conditioner window unit stopped cooling. I need an AC repair "
    "technician to come fix it next Tuesday."
)


def _seed_upcoming_ac_booking(status=None):
    store = load_store()
    start, end = _future_slot()
    session = Session(
        user_request=_AC_REQUEST,
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start, end=end)],
        booking=BookingRef(id="bk_ac", status=status),
        business=Business(name="HVAC-Koby", timezone="Asia/Jerusalem"),
        live_service={"name": "AC Installation Deposit"},
    )
    add_session(store, session)
    save_store(store)
    return len(store.sessions)


def _init_request(runner, request, *extra):
    return runner.invoke(
        app, ["flow", "init", "--user-request", request, *extra],
    )


def test_init_warns_about_a_matching_upcoming_booking_before_creating_a_session(
    runner, isolated_session,
):
    count = _seed_upcoming_ac_booking()
    r = _init_request(runner, _AC_REQUEST)

    assert r.exit_code == 0, r.stdout
    body = json.loads(r.stdout.strip())
    assert body["awaiting_user"] == "duplicate_booking_confirmation"
    match = body["data"]["upcoming_matches"][0]
    assert match["booking_id"] == "bk_ac"
    assert match["business_name"] == "HVAC-Koby"
    assert match["slot_start_display"].endswith("(Asia/Jerusalem)")
    assert "--allow-duplicate" in body["next_command"]
    assert "Never cancel" in body["prompt"]
    store = load_store()
    assert len(store.sessions) == count
    assert store.sessions[-1].booking.id == "bk_ac"


def test_init_with_allow_duplicate_starts_a_new_session(runner, isolated_session):
    count = _seed_upcoming_ac_booking()
    r = _init_request(runner, _AC_REQUEST, "--allow-duplicate")

    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout.strip()).get("awaiting_user") is None
    store = load_store()
    assert len(store.sessions) == count + 1
    assert any(s.booking.id == "bk_ac" and s.flow_phase == "CONFIRMED" for s in store.sessions)


@pytest.mark.parametrize(
    ("request_text", "status"),
    [
        ("I need a haircut tomorrow afternoon", None),
        (_AC_REQUEST, "canceled"),
    ],
    ids=["unrelated_request", "canceled_booking"],
)
def test_init_does_not_warn_without_a_matching_actionable_booking(
    runner, isolated_session, request_text, status,
):
    _seed_upcoming_ac_booking(status=status)
    r = _init_request(runner, request_text)

    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout.strip()).get("awaiting_user") is None


def test_save_store_omits_buyer_email_and_phone(isolated_session):
    from booking_agent_cli.session import session_path

    store = load_store()
    session = Session(
        buyer=Buyer(name="Ada", email="ada@secret.test", phone="+15551234567"),
        buyer_source="link-cli user-info retrieve",
    )
    add_session(store, session)
    save_store(store)
    raw = session_path().read_text()
    assert "ada@secret.test" not in raw
    assert "+15551234567" not in raw
    loaded = load_store()
    row = next(s for s in loaded.sessions if s.id == session.id)
    assert row.buyer.email == ""
    assert row.buyer.phone is None
    assert row.buyer.name == "Ada"


def test_require_buyer_rehydrates_contact_from_link_cli(isolated_session, monkeypatch):
    from booking_agent_cli.engine import require_buyer_from_link_cli
    from booking_agent_cli.session import BUYER_SOURCE_LINKCLI
    from support.mock_link_cli import mock_link_cli_user_info

    mock_link_cli_user_info(
        monkeypatch,
        {"name": "Ada Lovelace", "email": "ada@booking-agent.test", "phone": "+15550001111"},
    )
    session = Session(
        buyer_source=BUYER_SOURCE_LINKCLI,
        buyer=Buyer(name="Ada"),
    )
    require_buyer_from_link_cli(session)
    assert session.buyer.email == "ada@booking-agent.test"
    assert session.buyer.phone == "+15550001111"
