"""The confirmations the CLI enforces itself, so an agent that skips the skill cannot.

Policies, the booking summary, a less-direct result, and ``flow purge`` each need a
flag that counts only after an earlier call showed that exact content. The calendar
offer is enforced by ordering. Time and discovery fixes ask the user nothing.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from booking_agent_cli.session import Session, load_session, save_session
from integration.test_flow_gates import (
    _PROFILE_URL,
    _SERVICE_ID,
    _complete_checkout_mode_setup,
    _init,
    _invoke,
    _satisfy_link_auth_gate,
    _set_mock_payment_timing,
    _skip_calendar,
    _stdout_json,
)

_BOOK_CONFIRMED = ("flow", "book", "--service-id", _SERVICE_ID, "--user-confirmed")


def _error_code(result) -> str:
    return _stdout_json(result)["data"]["error"]["code"]


# ---------------------------------------------------------------------------
# What counts as a business policy
# ---------------------------------------------------------------------------

def _policy_session(**fields):
    base = dict(
        business=SimpleNamespace(timezone=None),
        live_service=None,
        ucp_checkout_links=None,
        ucp_checkout_policies=None,
        payment_schedules=None,
        payment_disclosures=None,
    )
    base.update(fields)
    return SimpleNamespace(**base)


def test_no_policies_when_the_business_sent_none():
    from booking_agent_cli.booking_summary import collect_provider_policies

    policies = collect_provider_policies(_policy_session(
        ucp_checkout_links=[{"rel": "continue", "url": "https://shop.test/checkout/1"}],
    ))
    assert policies == {"lines": [], "links": [], "digest": None}


def test_terms_and_privacy_links_are_shown_but_need_no_acceptance_of_their_own():
    from booking_agent_cli.booking_summary import collect_provider_policies

    links = [
        {"type": "terms_of_service", "url": "https://shop.test/terms"},
        {"type": "privacy_policy", "url": "https://shop.test/privacy"},
        {"rel": "continue", "url": "https://shop.test/checkout/1"},
        {"type": "terms_of_service", "url": "https://shop.test/terms"},
    ]
    policies = collect_provider_policies(_policy_session(ucp_checkout_links=links))

    assert [link["url"] for link in policies["links"]] == [
        "https://shop.test/terms", "https://shop.test/privacy",
    ]
    assert policies["digest"] is None
    with_text = collect_provider_policies(_policy_session(
        ucp_checkout_links=links,
        ucp_checkout_policies=[{"type": "refund", "description": {"plain": "No refunds."}}],
    ))
    assert with_text["lines"] == ["Policy: No refunds."]
    assert with_text["digest"]


def test_service_cancellation_and_rescheduling_rules_are_policies():
    from booking_agent_cli.booking_summary import collect_provider_policies

    live = {"policies": {
        "cancellation": {"allowed": True, "free_cancellation_until": "PT24H"},
        "rescheduling": {"allowed": False},
    }}
    policies = collect_provider_policies(_policy_session(live_service=live))

    assert policies["lines"] == [
        "Cancellation policy: Cancellation allowed, free up to 24 hours before the appointment.",
        "Rescheduling policy: Rescheduling is not allowed.",
    ]
    assert policies["digest"]


def test_a_policy_that_is_only_the_required_allowed_flag_is_not_a_business_policy():
    from booking_agent_cli.booking_summary import collect_provider_policies

    live = {"policies": {"cancellation": {"allowed": True}, "rescheduling": {"allowed": True}}}

    assert collect_provider_policies(_policy_session(live_service=live)) == {
        "lines": [], "links": [], "digest": None,
    }


_POLICY_LINES = ["Cancellation policy: Cancellation allowed, free up to 2 hours before the appointment."]


@pytest.fixture
def business_policies(monkeypatch):
    """The mock services send only ``allowed: true``; give the business a real policy."""
    import booking_agent_cli.booking_summary as booking_summary

    monkeypatch.setattr(booking_summary, "service_policy_lines", lambda _live: list(_POLICY_LINES))


# ---------------------------------------------------------------------------
# Direct bookings: the summary and the policies are accepted on `flow book`
# ---------------------------------------------------------------------------

def _direct_booking_to_recap(runner, monkeypatch) -> dict:
    _init(runner)
    _set_mock_payment_timing(monkeypatch, "at_service")
    r = _invoke(
        runner, "flow", "business", "use", "--profile-url", _PROFILE_URL, "--service-id", _SERVICE_ID,
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    _skip_calendar(runner)
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    r = _invoke(runner, "flow", "buyer", "resolve")
    assert r.exit_code == 0, r.stdout + r.stderr
    return _stdout_json(r)


def test_buyer_resolve_shows_policies_with_the_summary_on_a_direct_booking(
    runner, isolated_session, monkeypatch, business_policies,
):
    recap = _direct_booking_to_recap(runner, monkeypatch)

    assert recap["awaiting_user"] == "booking_confirmation"
    assert recap["next_command"] == "booking-agent flow book --user-confirmed --policies-accepted"
    assert any(line.startswith("Cancellation policy:") for line in recap["data"]["policy_lines"])
    session = load_session()
    assert session.recap_surfaced and session.recap_digest
    assert session.policies_surfaced and session.policies_digest


def test_direct_booking_without_policy_acceptance_books_nothing(
    runner, isolated_session, monkeypatch, business_policies,
):
    _direct_booking_to_recap(runner, monkeypatch)

    r = _invoke(runner, *_BOOK_CONFIRMED)

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "policies_not_accepted"
    assert body["data"]["awaiting_user"] == "policy_acceptance"
    assert body["next_command"].endswith("--policies-accepted")
    assert "Cancellation policy:" in body["user_message"]
    assert load_session().booking.id is None

    r = _invoke(runner, *_BOOK_CONFIRMED, "--policies-accepted")
    assert r.exit_code == 0, r.stdout + r.stderr
    session = load_session()
    assert session.booking.id
    assert session.policies_accepted_digest == session.policies_digest


def test_book_without_the_users_yes_to_the_summary_is_refused(
    runner, isolated_session, monkeypatch,
):
    _direct_booking_to_recap(runner, monkeypatch)

    r = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID, "--policies-accepted")

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "booking_confirmation_required"
    assert "--user-confirmed" in body["next_command"]
    assert load_session().booking.id is None


def test_a_summary_that_changed_after_the_user_saw_it_needs_a_new_yes(
    runner, isolated_session, monkeypatch,
):
    _direct_booking_to_recap(runner, monkeypatch)
    session = load_session()
    session.recap_digest = "0000000000000000"
    save_session(session)

    r = _invoke(runner, *_BOOK_CONFIRMED, "--policies-accepted")

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "booking_confirmation_required"
    assert body["next_command"].startswith("booking-agent flow buyer resolve")
    assert load_session().booking.id is None


def test_no_policy_step_when_the_business_set_no_policy(
    runner, isolated_session, monkeypatch,
):
    recap = _direct_booking_to_recap(runner, monkeypatch)

    assert recap["next_command"] == "booking-agent flow book --user-confirmed"
    assert "policy_lines" not in recap["data"]
    r = _invoke(runner, *_BOOK_CONFIRMED)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert load_session().booking.id


# ---------------------------------------------------------------------------
# Paid UCP bookings: the policies are accepted with the summary too
# ---------------------------------------------------------------------------

def _paid_booking_to_recap(runner) -> dict:
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _skip_calendar(runner)
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    r = _invoke(runner, "flow", "buyer", "resolve")
    assert r.exit_code == 0, r.stdout + r.stderr
    return _stdout_json(r)


_PAY = ("flow", "pay", "request", "--payment-method-id", "pm_test", "--context", "x" * 100)


def test_paid_checkout_asks_for_the_policies_with_the_summary_not_again_at_payment(
    runner, isolated_session, business_policies,
):
    recap = _paid_booking_to_recap(runner)
    assert recap["data"]["policy_lines"] == _POLICY_LINES
    assert recap["next_command"] == "booking-agent flow book --user-confirmed --policies-accepted"

    r = _invoke(runner, *_BOOK_CONFIRMED)
    assert r.exit_code == 1, r.stdout
    assert _error_code(r) == "policies_not_accepted"
    assert load_session().booking.id is None

    session = load_session()
    session.ucp_checkout_links = [{"type": "terms_of_service", "url": "https://shop.test/terms"}]
    save_session(session)
    r = _invoke(runner, *_BOOK_CONFIRMED, "--policies-accepted")
    assert r.exit_code == 0, r.stdout + r.stderr
    book = _stdout_json(r)
    assert book.get("awaiting_user") is None
    assert "--policies-accepted" not in book["next_command"]

    r = _invoke(runner, *_PAY)
    assert _error_code(r) != "policies_not_accepted"


def test_a_checkout_that_adds_policy_text_asks_again_before_payment(
    runner, isolated_session, business_policies,
):
    _paid_booking_to_recap(runner)
    r = _invoke(runner, *_BOOK_CONFIRMED, "--policies-accepted")
    assert r.exit_code == 0, r.stdout + r.stderr
    accepted = load_session().policies_accepted_digest
    session = load_session()
    session.ucp_checkout_policies = [{"type": "refund", "description": {"plain": "No refunds."}}]
    save_session(session)

    r = _invoke(runner, *_PAY, "--policies-accepted")

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "policies_not_accepted"
    assert "changed" in body["data"]["error"]["message"]
    assert "Policy: No refunds." in body["user_message"]
    assert load_session().policies_accepted_digest == accepted
    assert load_session().payment.spend_request_id is None

    r = _invoke(runner, *_PAY, "--policies-accepted")
    assert _error_code(r) != "policies_not_accepted"


# ---------------------------------------------------------------------------
# Less-direct results (mismatch confirmation)
# ---------------------------------------------------------------------------

def test_a_mismatch_the_cli_detects_stops_until_the_user_confirms(runner, isolated_session):
    _init(runner, user_request="AC repair, please come to my house in Yuma")
    args = ("flow", "business", "use", "--profile-url", _PROFILE_URL, "--service-id", _SERVICE_ID)

    premature = _invoke(runner, *args, "--mismatch-confirmed")
    assert premature.exit_code == 0, premature.stdout
    body = _stdout_json(premature)
    assert body["awaiting_user"] == "intent_mismatch_confirmation"
    assert body["data"]["mismatch_detected_by_cli"] is True
    assert body["data"]["error"]["code"] == "mismatch_not_surfaced"
    assert load_session().flow_phase != "BUSINESS_SELECTED"

    r = _invoke(runner, *args, "--mismatch-confirmed")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert load_session().selection_mismatch == body["data"]["less_direct"]


# ---------------------------------------------------------------------------
# flow purge
# ---------------------------------------------------------------------------

@pytest.fixture
def no_logout(monkeypatch):
    import booking_agent_cli.commands.flow as flow

    calls = []
    monkeypatch.setattr(flow, "logout_identity", lambda: calls.append(1) or {})
    return calls


def test_purge_confirmed_before_the_user_saw_the_list_deletes_nothing(
    runner, isolated_session, no_logout,
):
    _init(runner)

    r = _invoke(runner, "flow", "purge", "--user-confirmed")

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "purge_not_surfaced"
    assert "Do you want me to clear it all?" in body["user_message"]
    assert (isolated_session / ".usp-session.json").exists()
    assert no_logout == []


def test_purge_shows_the_list_then_clears_after_the_yes(runner, isolated_session, no_logout):
    _init(runner)

    shown = _invoke(runner, "flow", "purge")
    assert shown.exit_code == 0, shown.stdout
    body = _stdout_json(shown)
    assert body["awaiting_user"] == "purge_confirmation"
    assert body["next_command"] == "booking-agent flow purge --user-confirmed"
    assert (isolated_session / ".usp-session.json").exists()

    r = _invoke(runner, "flow", "purge", "--user-confirmed")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["user_message"].startswith("Done.")
    assert not (isolated_session / ".usp-session.json").exists()
    assert no_logout == [1]


def test_purge_asks_again_when_what_it_deletes_changed(runner, isolated_session, no_logout):
    _init(runner)
    _invoke(runner, "flow", "purge")
    _init(runner, user_request="a second request", force=True)

    r = _invoke(runner, "flow", "purge", "--user-confirmed")

    assert r.exit_code == 1, r.stdout
    assert _error_code(r) == "purge_not_surfaced"
    assert no_logout == []


# ---------------------------------------------------------------------------
# Calendar offer (ordering only) and time preference (no question to the user)
# ---------------------------------------------------------------------------

def test_calendar_skip_before_the_offer_is_refused(runner, isolated_session):
    _init(runner)
    _complete_checkout_mode_setup(runner)

    r = _invoke(runner, "flow", "calendar", "skip")

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "calendar_offer_not_shown"
    assert body["next_command"] == "booking-agent flow calendar ask"


def test_discover_refuses_a_named_day_without_a_time_flag(runner, isolated_session):
    _init(runner, user_request="AC repair in Yuma tomorrow")
    _invoke(runner, "flow", "prereq", "check")

    r = _invoke(runner, "flow", "discover")

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "time_preference_not_structured"
    assert body["data"]["named_time"] == "tomorrow"
    assert "--desired-range" in body["next_command"]

    r = _invoke(
        runner, "flow", "discover",
        "--desired-range", "2026-10-21T00:00:00-07:00,2026-10-21T23:59:00-07:00",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    session = load_session()
    assert session.time_preference_mode == "range"
    assert session.desired_time_ranges


def test_availability_starts_from_the_time_the_user_asked_for():
    from booking_agent_cli.commands.flow import _preferred_window

    session = Session(user_request="x")
    session.business.timezone = "America/Phoenix"
    session.desired_time_ranges = [
        {"start": "2026-10-21T08:00:00-07:00", "end": "2026-10-21T18:00:00-07:00"},
    ]
    assert _preferred_window(session) == (
        "2026-10-21T08:00:00-07:00", "2026-10-21T18:00:00-07:00",
    )
    session.desired_time_ranges = None
    assert _preferred_window(session) is None


def test_named_time_finds_days_and_clock_times():
    from booking_agent_cli.gates import named_time

    assert named_time("AC repair tomorrow afternoon") == "tomorrow"
    assert named_time("haircut at 3pm") == "3pm"
    assert named_time("AC repair in Yuma") is None


# ---------------------------------------------------------------------------
# Discovery that never shows the user a business it cannot book
# ---------------------------------------------------------------------------

def test_discover_drops_businesses_whose_profile_cannot_be_read(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow

    backend = flow.resolve_backend()
    original = backend.search_registry

    async def one_unreachable(intent):
        services = await original(intent)
        broken = services[0].model_copy(update={"metadata": {"profile_resolved": False}})
        return [broken, *services[1:]]

    monkeypatch.setattr(backend, "search_registry", one_unreachable)
    _init(runner)
    _invoke(runner, "flow", "prereq", "check")

    r = _invoke(runner, "flow", "discover")

    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["unreachable_profiles_filtered"] == 1
    assert all(row.get("profile_resolved") is not False for row in body["data"]["services"])


def test_discover_with_only_unreachable_businesses_says_so(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    backend = flow.resolve_backend()
    original = backend.search_registry

    async def all_unreachable(intent):
        return [
            s.model_copy(update={"metadata": {"profile_resolved": False}})
            for s in await original(intent)
        ]

    monkeypatch.setattr(backend, "search_registry", all_unreachable)
    _init(runner)
    _invoke(runner, "flow", "prereq", "check")

    r = _invoke(runner, "flow", "discover")

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "no_bookable_results"
    assert body["user_message"]


def test_an_unusable_business_profile_is_a_planned_outcome(runner, isolated_session, monkeypatch):
    from booking_agent_cli import support_report

    async def not_usp(_url):
        raise ValueError("UCP profile has no USP service binding or catalog capability")

    monkeypatch.setattr("booking_agent.ucp_profile.fetch_and_resolve_business_profile", not_usp)
    _init(runner)

    r = _invoke(
        runner, "flow", "business", "use", "--profile-url", _PROFILE_URL,
        "--service-id", _SERVICE_ID, "--name", "AZ Home Tech",
    )

    assert r.exit_code == 1, r.stdout
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "profile_resolve_failed"
    assert "no USP service binding" in body["data"]["error"]["message"]
    assert body["user_message"].startswith("AZ Home Tech can't take bookings")
    assert body["next_command"] == "booking-agent flow discover"
    assert body.get("awaiting_user") != "support_report_consent"
    assert support_report.offer_reason(
        load_session(), "profile_resolve_failed", body["next_command"],
    ) is None


@pytest.mark.parametrize("code", sorted(
    __import__("booking_agent_cli.support_report", fromlist=["x"]).AGENT_CONTRACT_CODES
))
def test_an_agent_ordering_mistake_never_offers_a_support_report(code):
    from booking_agent_cli import support_report

    assert support_report.offer_reason(Session(user_request="x"), code, None) is None


# ---------------------------------------------------------------------------
# Cleanup and output hygiene
# ---------------------------------------------------------------------------

def test_reset_emits_json_and_reports_a_missing_session(runner, isolated_session):
    _init(runner)

    r = _invoke(runner, "flow", "reset")
    assert r.exit_code == 0, r.stdout
    assert _stdout_json(r)["phase"] == "STORE"

    r = _invoke(runner, "flow", "reset")
    assert r.exit_code == 1
    assert _error_code(r) == "no_session"


def test_reset_deletes_the_calendar_event_of_an_unfinished_request(
    runner, isolated_session, monkeypatch,
):
    import booking_agent_cli.calendar_event as calendar_event
    import booking_agent_cli.commands.flow as flow

    removed = []
    monkeypatch.setattr(calendar_event, "existing_event_id", lambda _sid: "evt_1")
    monkeypatch.setattr(
        flow, "_remove_calendar_event_after_cancel",
        lambda session, **_k: removed.append(session.id) or {"status": "deleted"},
    )
    _init(runner)

    r = _invoke(runner, "flow", "reset")

    assert r.exit_code == 0, r.stdout
    assert _stdout_json(r)["data"]["calendar"] == {"status": "deleted"}
    assert len(removed) == 1


def test_forgetting_an_upcoming_booking_keeps_its_calendar_reminder(monkeypatch):
    import booking_agent_cli.calendar_event as calendar_event
    import booking_agent_cli.commands.flow as flow

    session = Session(user_request="x")
    forgotten = []
    monkeypatch.setattr(calendar_event, "existing_event_id", lambda _sid: "evt_1")
    monkeypatch.setattr(
        calendar_event, "_load_sidecar",
        lambda: {session.id: {"html_link": "https://calendar.google.com/event?eid=1"}},
    )
    monkeypatch.setattr(calendar_event, "_forget_event_id", forgotten.append)
    monkeypatch.setattr(flow, "is_actionable", lambda _s: True)
    monkeypatch.setattr(
        flow, "_remove_calendar_event_after_cancel",
        lambda _s: pytest.fail("an upcoming booking's event must stay"),
    )

    result = flow._calendar_cleanup_for_dropped_session(session)

    assert result["status"] == "kept"
    assert "https://calendar.google.com/event?eid=1" in result["user_note"]
    assert forgotten == [session.id]


def test_a_cancel_the_agent_cannot_do_tells_the_user_where_and_what_to_quote():
    from booking_agent_cli.commands.flow import _cancel_needs_human_message

    session = Session(user_request="x")
    session.business.name = "Koby's HVAC"
    session.live_service = {"policies": {"cancellation": {"allowed": True}}}
    err = SimpleNamespace(
        booking_url="https://koby.test/bookings/1",
        handoff={
            "order_id": "ord_9",
            "merchant_links": [
                {"type": "refund_policy", "url": "https://koby.test/refunds"},
                {"rel": "continue", "url": "https://koby.test/checkout"},
            ],
        },
    )

    message = _cancel_needs_human_message(session, "bkg_1", err)

    assert "https://koby.test/bookings/1" in message
    assert "Booking ID: bkg_1" in message
    assert "Order reference: ord_9" in message
    assert "Cancellation policy" not in message
    assert "https://koby.test/refunds" in message
    assert "https://koby.test/checkout" not in message


def test_money_path_events_stay_off_stderr_unless_asked(monkeypatch, capsys):
    from booking_agent.observability import emit_event

    monkeypatch.delenv("BOOKING_AGENT_STDERR_EVENTS", raising=False)
    emit_event("test_event", step="x")
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("BOOKING_AGENT_STDERR_EVENTS", "1")
    emit_event("test_event", step="x")
    assert '"event": "test_event"' in capsys.readouterr().err


# ---------------------------------------------------------------------------
# validate-skill-compliance
# ---------------------------------------------------------------------------

def _compliant_booked_session() -> Session:
    session = Session(user_request="AC repair")
    session.flow_phase = "BOOKED"
    session.calendar_gate_completed = True
    session.calendar_ask_surfaced = True
    session.link_auth_gate_completed = True
    session.checkout_prereq_completed = True
    session.slot_user_confirmed = True
    session.buyer_source = "linkcli"
    session.recap_digest = session.recap_confirmed_digest = "aaaa"
    session.policies_digest = session.policies_accepted_digest = "bbbb"
    return session


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ({"policies_accepted_digest": None}, "policies_accepted=False"),
        ({"recap_confirmed_digest": None}, "recap_confirmed=False"),
        ({"selection_mismatch": "at the shop", "mismatch_surfaced": False}, "mismatch_confirmed=False"),
        ({"calendar_ask_surfaced": False}, "calendar_offer_shown=False"),
        (
            {"user_request": "AC repair tomorrow", "time_preference_mode": "any"},
            "time_preference_structured=False",
        ),
    ],
)
def test_compliance_flags_a_booking_that_skipped_a_confirmation(change, failure):
    from booking_agent_cli.commands.flow import BUYER_SOURCE_LINKCLI, _skill_compliance_failures

    session = _compliant_booked_session()
    session.buyer_source = BUYER_SOURCE_LINKCLI
    assert _skill_compliance_failures(session) == []
    for key, value in change.items():
        setattr(session, key, value)

    failures = _skill_compliance_failures(session)

    assert any(f.startswith(failure) for f in failures), failures


# ---------------------------------------------------------------------------
# A new conversation resumes a booking that is waiting for the user's yes
# ---------------------------------------------------------------------------

def test_status_hands_back_the_summary_step_for_a_booking_waiting_for_the_yes(
    runner, isolated_session, monkeypatch,
):
    _direct_booking_to_recap(runner, monkeypatch)

    body = _stdout_json(_invoke(runner, "flow", "status"))

    assert body["phase"] == "BUYER_RESOLVED"
    assert body["awaiting_user"] == "booking_confirmation"
    assert body["next_command"] == "booking-agent flow buyer resolve"
    assert "start over" in body["prompt"]


def test_the_summary_can_be_shown_again_after_a_restart_and_then_confirmed(
    runner, isolated_session, monkeypatch,
):
    first = _direct_booking_to_recap(runner, monkeypatch)

    again = _invoke(runner, "flow", "buyer", "resolve")

    assert again.exit_code == 0, again.stdout + again.stderr
    body = _stdout_json(again)
    assert body["awaiting_user"] == "booking_confirmation"
    assert body["data"]["booking_recap_lines"] == first["data"]["booking_recap_lines"]
    assert load_session().recap_confirmed_digest is None
    r = _invoke(runner, *_BOOK_CONFIRMED)
    assert r.exit_code == 0, r.stdout + r.stderr
    assert load_session().booking.id


def test_the_next_command_after_a_changed_summary_actually_runs(
    runner, isolated_session, monkeypatch,
):
    _direct_booking_to_recap(runner, monkeypatch)
    session = load_session()
    session.recap_digest = "0000000000000000"
    save_session(session)
    refused = _stdout_json(_invoke(runner, *_BOOK_CONFIRMED))
    assert refused["next_command"].startswith("booking-agent flow buyer resolve")

    r = _invoke(runner, "flow", "buyer", "resolve")

    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["awaiting_user"] == "booking_confirmation"
