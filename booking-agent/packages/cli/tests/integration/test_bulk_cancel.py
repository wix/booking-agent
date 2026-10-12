"""``flow sessions cancel-open``: preview first, then cancel only what the user chose."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from booking_agent_cli.main import app
from booking_agent_cli.session import (
    BookingRef,
    Business,
    PaymentRef,
    Session,
    SlotRef,
    add_session,
    is_actionable,
    load_store,
    save_store,
    session_path,
)


def _slot(days: int) -> SlotRef:
    start = datetime.now(timezone.utc) + timedelta(days=days)
    return SlotRef(
        slot_id=f"slot-{days}",
        start=start.isoformat(),
        end=(start + timedelta(hours=1)).isoformat(),
    )


def _booked(booking_id: str, *, days: int = 2, status=None, request="haircut", **fields) -> Session:
    slot = _slot(days)
    return Session(
        user_request=request,
        flow_phase="CONFIRMED",
        business=Business(profile_url="https://biz.example/.well-known/ucp", name=f"Biz {booking_id}"),
        slot_id=slot.slot_id,
        slots_presented=[slot],
        booking=BookingRef(id=booking_id, status=status),
        **fields,
    )


def _unfinished(request: str = "piano lesson", **fields) -> Session:
    return Session(user_request=request, flow_phase="AVAILABILITY_QUERIED", **fields)


def _seed(*sessions: Session, active: Session | None = None) -> None:
    store = load_store()
    for s in sessions:
        add_session(store, s)
    if active is not None:
        store.active_session_id = active.id
    save_store(store)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://biz.example/usp/bookings/x/cancel")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=httpx.Response(status, request=request),
    )


class _Backend:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.cancelled: list[str] = []
        self.env_seen: dict[str, str | None] = {}

    async def resolve_profile(self, _profile_url):
        return SimpleNamespace(rest_endpoint="https://biz.example/usp")

    async def cancel_booking(self, _profile, booking_id, **_kwargs):
        from booking_agent import settings

        self.env_seen[booking_id] = settings.get("USP_PLATFORM_NAME")
        outcome = self.outcomes.get(booking_id)
        if isinstance(outcome, BaseException):
            raise outcome
        self.cancelled.append(booking_id)
        return SimpleNamespace(
            id=booking_id,
            status=SimpleNamespace(value="canceled"),
            cancellation=SimpleNamespace(fee=0, refund_amount=0),
        )


@pytest.fixture
def backend(isolated_session, monkeypatch):
    b = _Backend()
    monkeypatch.setattr("booking_agent_cli.commands.flow.resolve_backend", lambda: b)
    return b


def _invoke(runner: CliRunner, *args: str, exit_code: int = 0) -> dict:
    result = runner.invoke(app, ["flow", "sessions", "cancel-open", *args])
    text = result.stdout or result.output or ""
    assert result.exit_code == exit_code, f"stdout={text!r} exc={result.exception!r}"
    assert "Traceback" not in text
    obj, _ = json.JSONDecoder().raw_decode(text.strip())
    return obj


def _row(session_id: str) -> Session:
    return next(s for s in load_store().sessions if s.id == session_id)


def test_preview_is_read_only_and_groups_rows(runner, backend):
    upcoming = _booked("b1")
    canceled = _booked("b2", status="canceled")
    past = _booked("b3", days=-3)
    pending = _unfinished()
    paying = _unfinished("massage", payment=PaymentRef(spend_request_id="lsrq_1"))
    _seed(upcoming, canceled, past, pending, paying)
    before = session_path().read_text()

    data = _invoke(runner)["data"]

    assert data["mode"] == "preview"
    assert [r["booking_id"] for r in data["bookings"]] == ["b1"]
    assert [r["user_request"] for r in data["unfinished"]] == ["piano lesson"]
    assert [r["skip_reason"] for r in data["held"]] == ["payment_in_progress"]
    assert data["skipped"] == []
    assert backend.cancelled == []
    assert session_path().read_text() == before


def test_preview_with_selection_explains_skipped_rows(runner, backend):
    canceled = _booked("b2", status="completed")
    past = _booked("b3", days=-3)
    _seed(canceled, past)

    data = _invoke(runner, "--session-id", canceled.id, "--session-id", past.id)["data"]

    reasons = {r["session_id"]: r["skip_reason"] for r in data["skipped"]}
    assert reasons == {canceled.id: "booking_inactive", past.id: "booking_not_actionable"}


def test_selection_without_confirmation_stays_a_preview(runner, backend):
    upcoming = _booked("b1")
    _seed(upcoming)

    data = _invoke(runner, "--all")["data"]

    assert data["mode"] == "preview"
    assert backend.cancelled == []
    assert _row(upcoming.id).booking.status is None


def test_confirmation_requires_an_explicit_selection(runner, backend):
    _seed(_booked("b1"))

    data = _invoke(runner, "--user-confirmed", exit_code=1)["data"]

    assert data["error"]["code"] == "cancel_open_selection_required"
    assert backend.cancelled == []


def test_all_and_session_id_conflict(runner, backend):
    upcoming = _booked("b1")
    _seed(upcoming)

    data = _invoke(
        runner, "--user-confirmed", "--all", "--session-id", upcoming.id, exit_code=1,
    )["data"]

    assert data["error"]["code"] == "cancel_open_selection_conflict"
    assert backend.cancelled == []


def test_unknown_session_id_aborts_before_any_cancel(runner, backend):
    upcoming = _booked("b1")
    _seed(upcoming)

    data = _invoke(
        runner, "--user-confirmed", "--session-id", upcoming.id, "--session-id", "nope",
        exit_code=1,
    )["data"]

    assert data["error"]["code"] == "session_not_found"
    assert data["unknown_session_ids"] == ["nope"]
    assert backend.cancelled == []


def test_partial_failure_processes_every_booking_and_restores_active(runner, backend):
    ok = _booked("b_ok")
    forbidden = _booked("b_403")
    broken = _booked("b_500")
    finished = _booked("b_done", status="canceled")
    pending = _unfinished()
    _seed(ok, forbidden, broken, finished, pending, active=pending)
    backend.outcomes = {"b_403": _http_error(403), "b_500": _http_error(500)}

    data = _invoke(runner, "--user-confirmed", "--all")["data"]

    by_booking = {r["booking_id"]: r for r in data["results"]}
    assert set(by_booking) == {"b_ok", "b_403", "b_500"}
    assert by_booking["b_ok"]["outcome"] == "canceled"
    assert by_booking["b_403"]["outcome"] == "needs_human"
    assert by_booking["b_403"]["error"]["code"] == "cancel_forbidden"
    assert by_booking["b_403"]["cancel_urls"][0]["url"] == "https://biz.example"
    assert by_booking["b_500"]["outcome"] == "failed"
    assert by_booking["b_500"]["error"]["code"] == "cancel_failed"
    assert by_booking["b_500"]["http_status"] == 500
    assert data["summary"] == {"canceled": 1, "needs_human": 1, "failed": 1}
    assert backend.cancelled == ["b_ok"]

    assert _row(ok.id).booking.status == "canceled"
    assert not is_actionable(_row(ok.id))
    assert is_actionable(_row(forbidden.id))
    assert is_actionable(_row(broken.id))
    assert _row(pending.id).user_request == "piano lesson"
    assert load_store().active_session_id == pending.id
    assert data["active_session_id"] == pending.id


def test_explicit_selection_cancels_only_chosen_rows(runner, backend):
    chosen = _booked("b1", request="haircut")
    other = _booked("b2", request="lab test row")
    _seed(chosen, other)

    data = _invoke(runner, "--user-confirmed", "--session-id", chosen.id)["data"]

    assert [r["booking_id"] for r in data["results"]] == ["b1"]
    assert backend.cancelled == ["b1"]
    assert is_actionable(_row(other.id))


def test_unfinished_rows_need_forget_unfinished(runner, backend):
    pending = _unfinished()
    _seed(pending)

    data = _invoke(runner, "--user-confirmed", "--session-id", pending.id)["data"]
    assert data["results"][0]["outcome"] == "skipped"
    assert data["results"][0]["skip_reason"] == "forget_unfinished_not_authorized"
    assert any(s.id == pending.id for s in load_store().sessions)

    data = _invoke(runner, "--user-confirmed", "--all")["data"]
    assert data["results"] == []
    assert any(s.id == pending.id for s in load_store().sessions)

    data = _invoke(
        runner, "--user-confirmed", "--session-id", pending.id, "--forget-unfinished",
    )["data"]
    assert data["results"][0]["outcome"] == "forgotten"
    assert not any(s.id == pending.id for s in load_store().sessions)


def test_forget_unfinished_never_drops_payment_in_progress(runner, backend):
    paying = _unfinished("massage", payment=PaymentRef(captured_unconfirmed=True))
    pending = _unfinished()
    _seed(paying, pending)

    data = _invoke(runner, "--user-confirmed", "--all", "--forget-unfinished")["data"]

    outcomes = {r["session_id"]: r["outcome"] for r in data["results"]}
    assert outcomes == {paying.id: "skipped", pending.id: "forgotten"}
    ids = {s.id for s in load_store().sessions}
    assert paying.id in ids and pending.id not in ids


def test_each_session_gets_its_own_env_file(runner, backend, monkeypatch, tmp_path):
    from booking_agent_cli import config_loader

    monkeypatch.delenv("USP_PLATFORM_NAME", raising=False)
    config_loader.user_env_path().write_text("USP_PLATFORM_NAME=From the user env\n")
    env_file = tmp_path / "staging.env"
    env_file.write_text("USP_PLATFORM_NAME=From the env file\n")
    plain = _booked("b_plain")
    scoped = _booked("b_scoped", env_file=str(env_file))
    _seed(plain, scoped)

    data = _invoke(runner, "--user-confirmed", "--all")["data"]

    assert data["summary"] == {"canceled": 2}
    assert backend.env_seen["b_plain"] == "From the user env"
    assert backend.env_seen["b_scoped"] == "From the env file"
    assert "USP_PLATFORM_NAME" not in os.environ


def test_invalid_profile_url_fails_only_that_row(runner, backend, monkeypatch, tmp_path):
    monkeypatch.delenv("USP_AGENT_PROFILE_URL", raising=False)
    env_file = tmp_path / "bad.env"
    env_file.write_text("USP_AGENT_PROFILE_URL=http://profile.example/profile.json\n")
    good = _booked("b_good")
    bad = _booked("b_bad", env_file=str(env_file))
    _seed(good, bad)

    data = _invoke(runner, "--user-confirmed", "--all")["data"]

    by_booking = {r["booking_id"]: r for r in data["results"]}
    assert by_booking["b_bad"]["error"]["code"] == "platform_profile_url_invalid"
    assert by_booking["b_good"]["outcome"] == "canceled"


def test_bulk_cancel_gives_each_paid_row_the_same_refund_steps(runner, backend, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    paid = _booked(
        "b_paid",
        payment_route="at_booking",
        payment=PaymentRef(status="paid", order_id="o_9"),
        ucp_checkout_amount_minor=100,
        ucp_merchant_quote={"currency": "USD"},
    )
    free = _booked("b_free", payment_route="free")
    _seed(paid, free)

    out = _invoke(runner, "--user-confirmed", "--all")
    by_booking = {r["booking_id"]: r for r in out["data"]["results"]}

    message = by_booking["b_paid"]["user_message"]
    assert message.startswith("Your booking with Biz b_paid")
    assert "Biz b_paid did not refund the $1.00 you paid. To get it back:" in message
    assert "1. Go to Biz b_paid's website and find their contact details: https://biz.example" in message
    assert "2. Ask them to refund $1.00 for the cancelled booking." in message
    assert "3. Give them booking ID b_paid and order reference o_9" in message
    assert "refund" not in by_booking["b_free"]["user_message"].lower()
    assert "user_message" in out["prompt"]


def test_skill_documents_preview_first_and_no_row_inference():
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parents[4] / "skills" / "booking-agent" / "SKILL.md"
    ).read_text()
    section = skill.split("## Cancel upcoming bookings", 1)[1].split("\n## ", 1)[0]
    for term in (
        "booking-agent flow sessions cancel-open\n",
        "--user-confirmed --session-id",
        "--forget-unfinished",
        "Never** assume",
        "cancel_forbidden",
        "cancel_open_selection_required",
        "needs_human",
    ):
        assert term in section, term
    assert "\u2014" not in section and "\u2013" not in section
