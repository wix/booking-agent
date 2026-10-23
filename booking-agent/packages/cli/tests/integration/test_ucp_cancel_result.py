from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from booking_agent.rest_client import RevisionMismatch
from booking_agent.ucp_checkout import UcpCheckoutHttpError
from booking_agent_cli.commands.flow import _cancel_booking_async
from booking_agent_cli.main import app
from booking_agent_cli.session import BookingRef, Business, Session, add_session, load_store, save_store


@pytest.mark.asyncio
async def test_ucp_cancel_result_finishes_without_second_direct_cancel(monkeypatch):
    session = Session(
        business=Business(profile_url="https://biz.example/.well-known/ucp"),
        booking=BookingRef(id="b1"),
        ucp_checkout_session_id="cs_1",
        ucp_checkout_rest_base="https://biz.example/ucp",
    )
    direct_cancel_calls = 0

    class Backend:
        async def resolve_profile(self, _profile_url):
            return SimpleNamespace(rest_endpoint="https://biz.example/usp")

        async def cancel_booking(self, *_args, **_kwargs):
            nonlocal direct_cancel_calls
            direct_cancel_calls += 1
            raise AssertionError("UCP cancellation must not be repeated through USP")

    async def cancel_checkout(_session, *, credential):
        assert credential is None
        return {
            "status": "canceled",
            "booking": {
                "booking_id": "b1",
                "booking_status": "canceled",
            },
        }

    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.resolve_backend",
        lambda: Backend(),
    )
    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.cancel_ucp_checkout_async",
        cancel_checkout,
    )

    booking, fee = await _cancel_booking_async(session, "b1", None)

    assert booking.id == "b1"
    assert booking.status.value == "canceled"
    assert fee is None
    assert direct_cancel_calls == 0


def _install_abandoning_checkout(monkeypatch, *, cancel_statuses):
    """A business that cancels the checkout and leaves its pending booking (USP 7.5)."""
    calls = {"checkout": 0, "usp": 0}

    class Backend:
        async def resolve_profile(self, _profile_url):
            return SimpleNamespace(rest_endpoint="https://biz.example/usp")

        async def get_booking(self, *_args, **_kwargs):
            return SimpleNamespace(id="b1", status=SimpleNamespace(value="pending"))

        async def cancel_booking(self, *_args, **_kwargs):
            calls["usp"] += 1
            raise AssertionError("an abandoned checkout's booking must not be canceled")

    async def cancel_checkout(_session, *, credential):
        status = cancel_statuses[min(calls["checkout"], len(cancel_statuses) - 1)]
        calls["checkout"] += 1
        if status == 404:
            raise UcpCheckoutHttpError(404, '{"code":"not_found"}')
        return {"status": "canceled"}

    monkeypatch.setattr("booking_agent_cli.commands.flow.resolve_backend", lambda: Backend())
    monkeypatch.setattr("booking_agent_cli.commands.flow.cancel_ucp_checkout_async", cancel_checkout)
    return calls


def _open_checkout_session(**extra) -> Session:
    return Session(
        flow_phase="PAYMENT_PENDING",
        business=Business(name="HVAC Koby", profile_url="https://biz.example/.well-known/ucp"),
        booking=BookingRef(id="b1", status="pending"),
        ucp_checkout_session_id="cs_1",
        ucp_checkout_rest_base="https://biz.example/ucp",
        payment_route="pay_at_service",
        **extra,
    )


def test_canceled_checkout_without_booking_leaves_the_pending_booking(
    runner: CliRunner, isolated_session, monkeypatch,
):
    """Production body: ``{"status": "canceled"}``. Nothing to cancel behind it."""
    store = load_store()
    session = _open_checkout_session()
    session.booking.booking_url = "https://biz.example/bookings/b1"
    add_session(store, session)
    save_store(store)
    calls = _install_abandoning_checkout(monkeypatch, cancel_statuses=[200])

    obj = _invoke_cancel(runner, exit_code=0)

    assert calls == {"checkout": 1, "usp": 0}
    assert obj["data"]["status"] == "checkout_abandoned"
    assert obj["data"]["refund_follow_up"] is None
    assert "Nothing was booked and nothing was charged" in obj["user_message"]
    assert "refund" not in obj["user_message"].lower()
    assert "support_report_offered" not in obj
    row = next(s for s in load_store().sessions if s.id == session.id)
    assert row.flow_phase == "FAILED"
    assert row.booking.status == "pending"
    assert row.ucp_checkout_abandoned is True


def test_repeat_cancel_after_abandonment_never_reaches_usp(
    runner: CliRunner, isolated_session, monkeypatch,
):
    store = load_store()
    add_session(store, _open_checkout_session())
    save_store(store)
    calls = _install_abandoning_checkout(monkeypatch, cancel_statuses=[200, 404])

    _invoke_cancel(runner, exit_code=0)
    second = _invoke_cancel(runner, exit_code=0)

    assert second["data"]["status"] == "checkout_abandoned"
    assert calls == {"checkout": 1, "usp": 0}


@pytest.mark.asyncio
async def test_revision_conflict_on_a_confirmed_booking_retries_the_cancel(monkeypatch):
    """``confirmed`` ends confirmation polling. It does not end the booking.

    A 412 means the cancel was not applied. Re-reading a still-confirmed
    booking must retry against the new revision, not report the cancel done.
    """
    session = Session(
        business=Business(profile_url="https://biz.example/.well-known/ucp"),
        booking=BookingRef(id="b1", revision="r1"),
    )
    if_match_values = []

    class Backend:
        async def resolve_profile(self, _profile_url):
            return SimpleNamespace(rest_endpoint="https://biz.example/usp")

        async def get_booking(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="b1",
                status=SimpleNamespace(value="confirmed"),
                revision="r2",
            )

        async def cancel_booking(self, *_args, **kwargs):
            if_match_values.append(kwargs.get("if_match"))
            if kwargs.get("if_match") == "r1":
                raise RevisionMismatch("stale", sent_revision="r1")
            return SimpleNamespace(
                id="b1",
                status=SimpleNamespace(value="canceled"),
                revision="r3",
                cancellation=SimpleNamespace(fee=0, refund_amount=0),
            )

    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.resolve_backend",
        lambda: Backend(),
    )

    booking, settlement = await _cancel_booking_async(session, "b1", "changed plans")

    assert if_match_values == ["r1", "r2"]
    assert booking.status.value == "canceled"
    assert session.booking.revision == "r2"
    assert settlement["cancellation"] == {"fee": 0, "refund_amount": 0}


@pytest.mark.asyncio
async def test_revision_conflict_on_a_finished_booking_does_not_retry(monkeypatch):
    """A booking that is already canceled, completed, or a no-show stays that way."""
    session = Session(
        business=Business(profile_url="https://biz.example/.well-known/ucp"),
        booking=BookingRef(id="b1", revision="r1"),
    )
    cancel_calls = 0

    class Backend:
        async def resolve_profile(self, _profile_url):
            return SimpleNamespace(rest_endpoint="https://biz.example/usp")

        async def get_booking(self, *_args, **_kwargs):
            return SimpleNamespace(
                id="b1",
                status=SimpleNamespace(value="canceled"),
                revision="r2",
            )

        async def cancel_booking(self, *_args, **_kwargs):
            nonlocal cancel_calls
            cancel_calls += 1
            if cancel_calls == 1:
                raise RevisionMismatch("stale", sent_revision="r1")
            raise AssertionError("a finished booking must not be canceled again")

    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.resolve_backend",
        lambda: Backend(),
    )

    booking, settlement = await _cancel_booking_async(session, "b1", None)

    assert cancel_calls == 1
    assert booking.status.value == "canceled"
    assert settlement is None


def _completed_checkout_409_session() -> Session:
    return Session(
        flow_phase="CONFIRMED",
        business=Business(profile_url="https://biz.example/.well-known/ucp"),
        booking=BookingRef(id="b1"),
        ucp_checkout_session_id="cs_1",
        ucp_checkout_rest_base="https://biz.example/ucp",
        ucp_checkout_links=[
            {"type": "terms_of_service", "url": "https://biz.example/terms"},
        ],
        live_service={
            "policies": {
                "cancellation": {"allowed": True, "late_cancellation_fee": 0},
            },
        },
    )


def _install_completed_checkout_409(monkeypatch, *, checkout, booking_result):
    class Backend:
        async def resolve_profile(self, _profile_url):
            return SimpleNamespace(rest_endpoint="https://biz.example/usp")

        async def cancel_booking(self, *_args, **_kwargs):
            if isinstance(booking_result, Exception):
                raise booking_result
            return booking_result

    async def cancel_checkout(_session, *, credential):
        raise UcpCheckoutHttpError(
            409,
            '{"code":"invalid_state","message":"Checkout is completed and cannot be modified"}',
        )

    async def get_checkout(_session):
        if isinstance(checkout, Exception):
            raise checkout
        return checkout

    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.resolve_backend",
        lambda: Backend(),
    )
    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.cancel_ucp_checkout_async",
        cancel_checkout,
    )
    monkeypatch.setattr(
        "booking_agent_cli.payment_helpers.get_ucp_checkout_async",
        get_checkout,
    )


def _canceled_booking(booking_url="https://biz.example/bookings/b1"):
    return SimpleNamespace(
        id="b1",
        status=SimpleNamespace(value="canceled"),
        booking_url=booking_url,
        cancellation=SimpleNamespace(fee="0", refund_amount="0"),
        payment=SimpleNamespace(status="deposit_paid", deposit_amount="100", amount="1000"),
    )


def _invoke_cancel(runner: CliRunner, *, exit_code: int = 1):
    result = runner.invoke(app, ["flow", "book", "cancel"])
    text = result.stdout or result.output or ""
    assert result.exit_code == exit_code, f"stdout={text!r} exc={result.exception!r}"
    assert "Traceback" not in (text + (result.stderr or ""))
    text = text.strip()
    obj, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    return obj


def test_completed_checkout_409_cancels_the_booking_and_names_the_refund_step(
    runner: CliRunner, isolated_session, monkeypatch,
):
    """A completed checkout is immutable. The booking is not.

    USP §5.3.5 is the cancel, and it reports the refund separately. The hosted
    site does the same split via Manage Booking, so the agent cancels and then
    tells the buyer to ask the business for the money.
    """
    store = load_store()
    session = _completed_checkout_409_session()
    session.due_now_amount_minor = 100
    add_session(store, session)
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch,
        checkout={
            "order": {"id": "o_1", "permalink_url": "https://biz.example/orders/o_1"},
        },
        booking_result=_canceled_booking(),
    )

    data = _invoke_cancel(runner, exit_code=0)["data"]

    assert data["status"] == "canceled"
    assert data["cancellation"] == {"fee": 0, "refund_amount": 0}
    follow_up = data["refund_follow_up"]
    assert follow_up["required"] is True
    assert follow_up["captured_amount_minor"] == 100
    assert follow_up["refund_amount"] == 0
    # booking_url is the protocol's manage page: the same link the confirmation
    # email's Manage Booking button opens.
    assert follow_up["manage_url"] == "https://biz.example/bookings/b1"
    assert follow_up["contact_url"] == "https://biz.example/bookings/b1"
    assert follow_up["order_id"] == "o_1"
    assert "order_url" not in follow_up


def test_refund_follow_up_without_manage_page_points_at_the_business_site(
    runner: CliRunner, isolated_session, monkeypatch,
):
    """The order permalink is often a session-bound thank-you page that renders blank later."""
    store = load_store()
    session = _completed_checkout_409_session()
    session.due_now_amount_minor = 100
    add_session(store, session)
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch,
        checkout={
            "order": {"id": "o_1", "permalink_url": "https://biz.example/thank-you-page/o_1"},
        },
        booking_result=_canceled_booking(booking_url=None),
    )

    obj = _invoke_cancel(runner, exit_code=0)
    follow_up = obj["data"]["refund_follow_up"]

    assert follow_up["manage_url"] is None
    assert follow_up["contact_url"] == "https://biz.example"
    assert "thank-you-page" not in json.dumps(obj)
    message = obj["user_message"]
    assert "website and find their contact details: https://biz.example" in message
    assert "order reference o_1" in message


def test_successful_cancel_persists_status_and_stops_being_actionable(
    runner: CliRunner, isolated_session, monkeypatch,
):
    from datetime import datetime, timedelta, timezone

    from booking_agent_cli.session import SlotRef, is_actionable

    store = load_store()
    session = _completed_checkout_409_session()
    start = datetime.now(timezone.utc) + timedelta(days=1)
    session.slot_id = "slot-1"
    session.slots_presented = [
        SlotRef(
            slot_id="slot-1",
            start=start.isoformat(),
            end=(start + timedelta(hours=1)).isoformat(),
        ),
    ]
    add_session(store, session)
    save_store(store)
    assert is_actionable(session)
    _install_completed_checkout_409(
        monkeypatch,
        checkout={},
        booking_result=_canceled_booking(),
    )

    _invoke_cancel(runner, exit_code=0)

    row = next(s for s in load_store().sessions if s.id == session.id)
    assert row.booking.status == "canceled"
    assert not is_actionable(row)


def test_status_after_cancel_does_not_say_the_booking_is_still_booked(
    runner: CliRunner, isolated_session, monkeypatch,
):
    """flow status keeps phase CONFIRMED, but must not tell the user they are booked."""
    store = load_store()
    session = _completed_checkout_409_session()
    session.business.name = "HVAC-Koby"
    session.live_service = {**session.live_service, "name": "AC Repair"}
    session.calendar_event_html_link = "https://www.google.com/calendar/event?eid=abc"
    add_session(store, session)
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch,
        checkout={},
        booking_result=_canceled_booking(),
    )
    _invoke_cancel(runner, exit_code=0)

    result = runner.invoke(app, ["flow", "status"])
    text = result.stdout or result.output or ""
    assert result.exit_code == 0, text
    body, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    message = body["user_message"]

    assert body["phase"] == "CONFIRMED"
    assert "cancelled" in message
    assert "AC Repair with HVAC-Koby" in message
    assert not message.startswith("You're booked")
    assert "calendar/event" not in message
    assert "confirmed" not in body["prompt"].lower()


def test_completed_checkout_409_still_cancels_when_the_checkout_cannot_be_reread(
    runner: CliRunner, isolated_session, monkeypatch,
):
    store = load_store()
    add_session(store, _completed_checkout_409_session())
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch,
        checkout=UcpCheckoutHttpError(503, "upstream down"),
        booking_result=_canceled_booking(),
    )

    data = _invoke_cancel(runner, exit_code=0)["data"]

    assert data["status"] == "canceled"
    assert data["refund_follow_up"]["manage_url"] == "https://biz.example/bookings/b1"
    assert data["refund_follow_up"]["contact_url"] == "https://biz.example/bookings/b1"


def test_booking_url_directs_the_buyer_and_skips_the_credentialed_cancel(
    runner: CliRunner, isolated_session, monkeypatch,
):
    store = load_store()
    session = _completed_checkout_409_session()
    session.booking.booking_url = "https://biz.example/bookings/b1"
    add_session(store, session)
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch,
        checkout={"order": {"permalink_url": "https://biz.example/orders/o_1"}},
        booking_result=AssertionError("POST /bookings/{id}/cancel must not run"),
    )

    obj = _invoke_cancel(runner)
    data = obj["data"]

    assert data["error"]["code"] == "cancel_at_business"
    assert data["booking_url"] == "https://biz.example/bookings/b1"
    assert data["cancel_urls"][0] == {
        "type": "booking",
        "url": "https://biz.example/bookings/b1",
    }
    assert "https://biz.example/bookings/b1" in obj["prompt"]


def test_a_booking_url_read_from_the_business_also_skips_the_cancel(
    runner: CliRunner, isolated_session, monkeypatch,
):
    """The session may have missed the URL. The live booking is authoritative."""
    store = load_store()
    add_session(store, _completed_checkout_409_session())
    save_store(store)

    class Backend:
        async def resolve_profile(self, _profile_url):
            return SimpleNamespace(rest_endpoint="https://biz.example/usp")

        async def get_booking(self, *_args, **_kwargs):
            return SimpleNamespace(booking_url="https://biz.example/manage/b1")

        async def cancel_booking(self, *_args, **_kwargs):
            raise AssertionError("POST /bookings/{id}/cancel must not run")

    async def cancel_checkout(_session, *, credential):
        raise UcpCheckoutHttpError(409, '{"code":"invalid_state"}')

    async def get_checkout(_session):
        return {}

    monkeypatch.setattr("booking_agent_cli.commands.flow.resolve_backend", lambda: Backend())
    monkeypatch.setattr("booking_agent_cli.commands.flow.cancel_ucp_checkout_async", cancel_checkout)
    monkeypatch.setattr("booking_agent_cli.payment_helpers.get_ucp_checkout_async", get_checkout)

    data = _invoke_cancel(runner)["data"]

    assert data["error"]["code"] == "cancel_at_business"
    assert data["booking_url"] == "https://biz.example/manage/b1"


def test_absent_booking_url_still_uses_the_credentialed_cancel_and_its_401(
    runner: CliRunner, isolated_session, monkeypatch,
):
    from booking_agent.rest_client import PopAuthorizationError

    store = load_store()
    add_session(store, _completed_checkout_409_session())
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch,
        checkout={"order": {"permalink_url": "https://biz.example/orders/o_1"}},
        booking_result=PopAuthorizationError("pop_proof_invalid", "nope"),
    )

    data = _invoke_cancel(runner)["data"]

    assert data["error"]["code"] == "pop_proof_invalid"
    assert data["booking_url"] is None
    assert data["cancel_urls"][0]["url"] == "https://biz.example"


def _http_status_error(status: int):
    import httpx

    request = httpx.Request("POST", "https://biz.example/usp/bookings/b1/cancel")
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=request,
        response=httpx.Response(status, request=request),
    )


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "cancel_unauthorized"), (403, "cancel_forbidden")],
)
def test_rejected_credential_hands_off_without_a_traceback(
    runner: CliRunner, isolated_session, monkeypatch, status, code,
):
    store = load_store()
    add_session(store, _completed_checkout_409_session())
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch,
        checkout={"order": {"permalink_url": "https://biz.example/orders/o_1"}},
        booking_result=_http_status_error(status),
    )

    data = _invoke_cancel(runner)["data"]

    assert data["error"]["code"] == code
    assert data["http_status"] == status
    assert data["cancel_urls"][0] == {
        "type": "merchant_site",
        "url": "https://biz.example",
    }
    assert all(u["type"] != "order" for u in data["cancel_urls"])


def test_ucp_checkout_403_hands_off_before_the_usp_cancel(
    runner: CliRunner, isolated_session, monkeypatch,
):
    store = load_store()
    add_session(store, _completed_checkout_409_session())
    save_store(store)

    class Backend:
        async def resolve_profile(self, _profile_url):
            return SimpleNamespace(rest_endpoint="https://biz.example/usp")

        async def cancel_booking(self, *_args, **_kwargs):
            raise AssertionError("a rejected checkout cancel must not fall through")

    async def cancel_checkout(_session, *, credential):
        raise UcpCheckoutHttpError(403, '{"code":"forbidden"}')

    monkeypatch.setattr("booking_agent_cli.commands.flow.resolve_backend", lambda: Backend())
    monkeypatch.setattr("booking_agent_cli.commands.flow.cancel_ucp_checkout_async", cancel_checkout)

    data = _invoke_cancel(runner)["data"]

    assert data["error"]["code"] == "cancel_forbidden"
    assert data["cancel_urls"][0]["type"] == "merchant_site"
    assert data["cancel_urls"][0]["url"] == "https://biz.example"


@pytest.mark.parametrize("status", [404, 422, 500, 503])
def test_other_http_failures_are_structured_cancel_failed(
    runner: CliRunner, isolated_session, monkeypatch, status,
):
    store = load_store()
    session = _completed_checkout_409_session()
    add_session(store, session)
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch,
        checkout={},
        booking_result=_http_status_error(status),
    )

    data = _invoke_cancel(runner)["data"]

    assert data["error"]["code"] == "cancel_failed"
    assert data["http_status"] == status
    assert data["booking_id"] == "b1"
    assert data["cancel_urls"]
    row = next(s for s in load_store().sessions if s.id == session.id)
    assert row.booking.status is None


def test_unreachable_business_is_structured_cancel_failed(
    runner: CliRunner, isolated_session, monkeypatch,
):
    import httpx

    store = load_store()
    add_session(store, _completed_checkout_409_session())
    save_store(store)

    class Backend:
        async def resolve_profile(self, _profile_url):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("booking_agent_cli.commands.flow.resolve_backend", lambda: Backend())

    data = _invoke_cancel(runner)["data"]

    assert data["error"]["code"] == "cancel_failed"
    assert "http_status" not in data


def _paid_at_booking_session(**overrides) -> Session:
    from booking_agent_cli.session import PaymentRef

    session = _completed_checkout_409_session()
    session.business.name = "HVAC-Koby"
    session.payment_route = "at_booking"
    session.payment = PaymentRef(status="paid", order_id="o_1")
    session.ucp_checkout_amount_minor = 100
    session.ucp_merchant_quote = {"currency": "USD", "total_minor_units": 100}
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def _canceled_without_payment_echo(refund_amount="0", booking_url=None):
    return SimpleNamespace(
        id="b1",
        status=SimpleNamespace(value="canceled"),
        booking_url=booking_url,
        cancellation=SimpleNamespace(fee="0", refund_amount=refund_amount),
        payment=None,
    )


def _cancel_paid_session(runner, monkeypatch, session, booking_result):
    store = load_store()
    add_session(store, session)
    save_store(store)
    _install_completed_checkout_409(
        monkeypatch, checkout={"order": {"id": "o_1"}}, booking_result=booking_result,
    )
    return _invoke_cancel(runner, exit_code=0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN_CACHE_DIR", raising=False)
    return tmp_path


def test_paid_at_booking_zero_refund_tells_the_buyer_how_to_get_the_money_back(
    runner: CliRunner, isolated_session, home, monkeypatch,
):
    """The chat this came from: $1.00 paid at booking, the business refunded 0."""
    obj = _cancel_paid_session(
        runner, monkeypatch, _paid_at_booking_session(), _canceled_without_payment_echo(),
    )
    follow_up = obj["data"]["refund_follow_up"]

    assert follow_up["captured_amount_minor"] == 100
    assert follow_up["outstanding_amount_minor"] == 100
    assert follow_up["outstanding_display"] == "$1.00"
    assert follow_up["contact_url"] == "https://biz.example"
    assert follow_up["booking_id"] == "b1"
    assert follow_up["order_id"] == "o_1"
    assert follow_up["steps"] == [
        "Go to HVAC-Koby's website and find their contact details: https://biz.example",
        "Ask them to refund $1.00 for the cancelled booking.",
        "Give them booking ID b1 and order reference o_1 so they can find your payment.",
    ]
    message = obj["user_message"]
    assert message.startswith("Your booking with HVAC-Koby")
    assert "HVAC-Koby did not refund the $1.00 you paid. To get it back:" in message
    for i, step in enumerate(follow_up["steps"], 1):
        assert f"{i}. {step}" in message
    assert "user_message" in obj["prompt"]


def test_unreported_refund_still_lists_the_steps(
    runner: CliRunner, isolated_session, home, monkeypatch,
):
    obj = _cancel_paid_session(
        runner, monkeypatch, _paid_at_booking_session(),
        _canceled_without_payment_echo(refund_amount=None, booking_url="https://biz.example/m/b1"),
    )
    follow_up = obj["data"]["refund_follow_up"]

    assert follow_up["refund_amount"] is None
    assert follow_up["steps"][0] == "Open HVAC-Koby's booking page: https://biz.example/m/b1"
    assert "did not say whether the $1.00 you paid will be refunded" in obj["user_message"]


def test_full_refund_needs_no_follow_up_but_says_it_is_coming(
    runner: CliRunner, isolated_session, home, monkeypatch,
):
    obj = _cancel_paid_session(
        runner, monkeypatch, _paid_at_booking_session(),
        _canceled_without_payment_echo(refund_amount="100"),
    )

    assert obj["data"]["refund_follow_up"] is None
    assert "HVAC-Koby is refunding the $1.00 you paid." in obj["user_message"]


@pytest.mark.parametrize("route", ["free", "at_service"])
def test_routes_that_collected_nothing_have_no_refund_steps(
    runner: CliRunner, isolated_session, home, monkeypatch, route,
):
    obj = _cancel_paid_session(
        runner, monkeypatch, _paid_at_booking_session(payment_route=route),
        _canceled_without_payment_echo(),
    )

    assert obj["data"]["refund_follow_up"] is None
    assert "refund" not in obj["user_message"].lower()


def test_confirmed_pay_at_service_cancel_asks_for_no_refund(
    runner: CliRunner, isolated_session, home, monkeypatch,
):
    """The order id of an offline checkout references the booking; nothing was charged."""
    from booking_agent_cli.session import PaymentRef

    obj = _cancel_paid_session(
        runner, monkeypatch,
        _paid_at_booking_session(
            payment_route="pay_at_service",
            payment=PaymentRef(status="not_required", order_id="o_1"),
        ),
        _canceled_without_payment_echo(),
    )

    assert obj["data"]["refund_follow_up"] is None
    assert "refund" not in obj["user_message"].lower()


def _fake_google_delete(monkeypatch, status_code: int):
    calls = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def delete(self, url, headers=None):
            calls.append(url)
            return SimpleNamespace(status_code=status_code, text="")

    monkeypatch.setattr("booking_agent_cli.calendar_event.httpx.Client", FakeClient)
    return calls


def test_cancel_removes_the_google_calendar_event(
    runner: CliRunner, isolated_session, home, monkeypatch,
):
    from booking_agent_cli.calendar_event import existing_event_id, record_event_id, write_token_cache

    session = _paid_at_booking_session()
    record_event_id(session.id, "evt-1", "https://calendar.google.com/event?eid=evt-1")
    write_token_cache("cli", "tok-write", expires_in=3600)
    calls = _fake_google_delete(monkeypatch, 204)

    obj = _cancel_paid_session(runner, monkeypatch, session, _canceled_without_payment_echo())

    assert len(calls) == 1 and calls[0].endswith("/evt-1")
    assert obj["data"]["calendar"]["status"] == "deleted"
    assert existing_event_id(session.id) is None
    assert obj["user_message"].endswith("I also removed it from your Google Calendar.")


def test_calendar_failure_never_undoes_the_cancel(
    runner: CliRunner, isolated_session, home, monkeypatch,
):
    from booking_agent_cli.calendar_event import existing_event_id, record_event_id, write_token_cache

    session = _paid_at_booking_session()
    record_event_id(session.id, "evt-1", "https://calendar.google.com/event?eid=evt-1")
    write_token_cache("cli", "tok-write", expires_in=3600)
    _fake_google_delete(monkeypatch, 500)

    obj = _cancel_paid_session(runner, monkeypatch, session, _canceled_without_payment_echo())

    assert obj["data"]["status"] == "canceled"
    assert obj["data"]["calendar"]["status"] == "failed"
    assert existing_event_id(session.id) == "evt-1"
    assert (
        "please delete that event yourself: https://calendar.google.com/event?eid=evt-1"
        in obj["user_message"]
    )
    row = next(s for s in load_store().sessions if s.id == session.id)
    assert row.booking.status == "canceled"


def test_cancel_without_a_calendar_event_says_nothing_about_the_calendar(
    runner: CliRunner, isolated_session, home, monkeypatch,
):
    calls = _fake_google_delete(monkeypatch, 204)

    obj = _cancel_paid_session(
        runner, monkeypatch, _paid_at_booking_session(), _canceled_without_payment_echo(),
    )

    assert calls == []
    assert obj["data"]["calendar"] is None
    assert "Calendar" not in obj["user_message"]


@pytest.mark.parametrize("status", ["canceled", "completed", "no_show"])
def test_cancel_of_an_already_finished_booking_does_not_call_the_business(
    runner: CliRunner, isolated_session, monkeypatch, status,
):
    """A successful cancel retires the credential, so a repeat cancel can only be rejected.

    The business answers that repeat with 401 ``pop_key_mismatch``, which reads as
    if the cancel never happened. The recorded status already settles it.
    """
    store = load_store()
    session = _completed_checkout_409_session()
    session.booking.status = status
    add_session(store, session)
    save_store(store)

    def no_backend():
        raise AssertionError("a finished booking must not be sent to the business again")

    monkeypatch.setattr("booking_agent_cli.commands.flow.resolve_backend", no_backend)

    obj = _invoke_cancel(runner, exit_code=0)

    assert obj["data"]["status"] == status
    assert obj["data"]["calendar"] is None
    assert obj["user_message"] == (
        f"This booking is already {status.replace('_', ' ')}, so there was nothing to cancel."
    )
