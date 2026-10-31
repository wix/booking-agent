"""Buyer resolve opens the business's checkout once, so the summary shows its real terms."""
import pytest

from integration.test_flow_gates import (
    _BOOK,
    _SERVICE_ID,
    _complete_checkout_mode_setup,
    _init,
    _invoke,
    _satisfy_link_auth_gate,
    _skip_calendar,
    _stdout_json,
)

_REST_BASE = "https://mock-ucp.test/checkout"


@pytest.fixture
def checkouts(isolated_session, monkeypatch):
    """Record every checkout create and cancel the CLI makes."""
    import booking_agent_cli.payment_helpers as ph

    inner = ph._create_booking_via_ucp_checkout
    log = {"created": [], "reused": [], "keys": [], "cancelled": [], "cancel_status": "canceled",
           "fail_next_create": False, "total": 100}

    async def create(session, service_id, notes, delivery_address=None, *,
                     reuse_checkout_id=None, update_checkout_id=None):
        log["keys"].append(session.checkout_create_idempotency_key)
        if reuse_checkout_id:
            log["reused"].append(reuse_checkout_id)
        if log["fail_next_create"]:
            log["fail_next_create"] = False
            raise RuntimeError("connection reset after the request was sent")
        out = await inner(session, service_id, notes, delivery_address,
                          reuse_checkout_id=reuse_checkout_id)
        if reuse_checkout_id:
            session.ucp_checkout_session_id = reuse_checkout_id
        else:
            log["created"].append({"notes": notes, "slot_id": session.slot_id})
            session.ucp_checkout_session_id = f"chk_{len(log['created'])}"
        session.ucp_checkout_rest_base = _REST_BASE
        session.ucp_checkout_amount_minor = log["total"]
        session.ucp_merchant_quote = {"currency": "USD", "total_minor_units": log["total"]}
        return out

    async def cancel(session):
        log["cancelled"].append(session.ucp_checkout_session_id)
        if log["cancel_status"] == "raise":
            raise RuntimeError("timed out")
        return {"id": session.ucp_checkout_session_id, "status": log["cancel_status"]}

    monkeypatch.setattr(ph, "_create_booking_via_ucp_checkout", create)
    monkeypatch.setattr(ph, "_cancel_checkout_for_abandonment", cancel)
    return log


def _ok(result):
    assert result.exit_code == 0, result.stdout + result.stderr
    return _stdout_json(result)


def _err(result, code):
    assert result.exit_code == 1, result.stdout + result.stderr
    body = _stdout_json(result)
    assert body["data"]["error"]["code"] == code, body
    return body


def _to_summary(runner, *resolve_args):
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _skip_calendar(runner)
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    slots = _ok(_invoke(runner, "flow", "slots", "list"))
    _ok(_invoke(runner, "flow", "slots", "select", "--slot-id",
                slots["data"]["slots"][0]["slot_id"], "--user-confirmed"))
    _satisfy_link_auth_gate()
    return _ok(_invoke(runner, "flow", "buyer", "resolve", *resolve_args))


def test_the_summary_comes_from_one_checkout_that_flow_book_takes(runner, checkouts):
    summary = _to_summary(runner)
    assert summary["awaiting_user"] == "booking_confirmation"
    assert summary["data"]["checkout"]["ucp_checkout_session_id"] == "chk_1"
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    booked = _ok(_invoke(runner, *_BOOK))
    assert len(checkouts["created"]) == 1
    assert checkouts["reused"] and set(checkouts["reused"]) == {"chk_1"}
    assert checkouts["cancelled"] == []
    assert booked.get("awaiting_user") not in ("policy_acceptance", "booking_confirmation")


def test_a_lost_create_response_is_retried_with_the_same_key(runner, checkouts):
    checkouts["fail_next_create"] = True
    _init(runner)
    _complete_checkout_mode_setup(runner)
    _skip_calendar(runner)
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    slots = _ok(_invoke(runner, "flow", "slots", "list"))
    _ok(_invoke(runner, "flow", "slots", "select", "--slot-id",
                slots["data"]["slots"][0]["slot_id"], "--user-confirmed"))
    _satisfy_link_auth_gate()
    _err(_invoke(runner, "flow", "buyer", "resolve"), "checkout_prepare_failed")
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    assert checkouts["keys"][0] and checkouts["keys"][0] == checkouts["keys"][1]
    assert len(checkouts["created"]) == 1


def test_a_changed_note_releases_the_old_checkout_and_opens_a_new_one(runner, checkouts):
    _to_summary(runner, "--notes", "Unit is silent")
    first_key = checkouts["keys"][-1]
    again = _ok(_invoke(runner, "flow", "buyer", "resolve", "--notes", "Unit leaks water"))
    assert checkouts["cancelled"] == ["chk_1"]
    assert [c["notes"] for c in checkouts["created"]] == ["Unit is silent", "Unit leaks water"]
    assert checkouts["keys"][-1] != first_key
    assert again["data"]["checkout"]["ucp_checkout_session_id"] == "chk_2"


def test_a_lapsed_checkout_with_the_same_terms_books_without_asking_again(runner, checkouts):
    from booking_agent_cli.session import must_load, save_session

    _to_summary(runner)
    session = must_load()
    session.prepared_checkout_expires_at = "2020-01-01T00:00:00Z"
    save_session(session)
    _ok(_invoke(runner, *_BOOK))
    assert checkouts["cancelled"] == ["chk_1"]
    assert len(checkouts["created"]) == 2
    assert must_load().flow_phase == "PAYMENT_PENDING"


def test_a_lapsed_checkout_with_new_terms_asks_again_before_any_charge(runner, checkouts):
    from booking_agent_cli.session import must_load, save_session

    _to_summary(runner)
    session = must_load()
    session.prepared_checkout_expires_at = "2020-01-01T00:00:00Z"
    save_session(session)
    checkouts["total"] = 250
    _err(_invoke(runner, *_BOOK), "booking_confirmation_required")
    assert must_load().flow_phase == "BUYER_RESOLVED"
    summary = _ok(_invoke(runner, "flow", "buyer", "resolve"))
    assert summary["data"]["checkout"]["merchant_quote"]["total_minor_units"] == 250


def test_the_snapshot_digest_covers_the_business_links():
    from booking_agent_cli.payment_helpers import approval_snapshot, snapshot_digest
    from booking_agent_cli.session import Session

    s = Session()
    s.ucp_merchant_quote = {"currency": "USD", "total_minor_units": 100}
    s.ucp_checkout_links = [{"type": "terms_of_service", "url": "https://biz.test/terms"}]
    before = snapshot_digest(approval_snapshot(s))
    s.ucp_checkout_links = [{"type": "terms_of_service", "url": "https://biz.test/terms-v2"}]
    assert snapshot_digest(approval_snapshot(s)) != before


def test_saying_no_releases_the_checkout_and_keeps_the_time(runner, checkouts):
    from booking_agent_cli.session import must_load

    _to_summary(runner)
    slot_id = must_load().slot_id
    declined = _ok(_invoke(runner, "flow", "book", "decline"))
    assert checkouts["cancelled"] == ["chk_1"]
    session = must_load()
    assert session.flow_phase == "BUYER_RESOLVED"
    assert session.slot_id == slot_id
    assert session.ucp_checkout_session_id is None
    assert "released the checkout, and nothing was charged" in declined["user_message"]


def test_a_cancel_the_business_does_not_confirm_keeps_the_session(runner, checkouts):
    from booking_agent_cli.session import must_load

    _to_summary(runner)
    checkouts["cancel_status"] = "ready_for_complete"
    body = _err(_invoke(runner, "flow", "book", "decline"), "checkout_cleanup_failed")
    assert body["user_message"].startswith("I couldn't confirm")
    assert "cancel" not in body["user_message"].lower()
    assert must_load().ucp_checkout_session_id == "chk_1"
    checkouts["cancel_status"] = "raise"
    _err(_invoke(runner, "flow", "reset"), "checkout_cleanup_failed")
    assert must_load().ucp_checkout_session_id == "chk_1"


@pytest.mark.parametrize("command", [
    ("flow", "reset"),
    ("flow", "business", "use", "--profile-url", "https://other.booking-agent.test/.well-known/usp"),
])
def test_leaving_the_summary_releases_the_checkout(runner, checkouts, command):
    _to_summary(runner)
    _invoke(runner, *command)
    assert checkouts["cancelled"] == ["chk_1"]


def test_picking_another_time_releases_the_checkout(runner, checkouts):
    _to_summary(runner)
    _ok(_invoke(runner, "flow", "availability", "--change-time", "--service-id", _SERVICE_ID))
    slots = _ok(_invoke(runner, "flow", "slots", "list"))
    other = slots["data"]["slots"][1]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", other, "--user-confirmed")
    assert checkouts["cancelled"] == ["chk_1"]
