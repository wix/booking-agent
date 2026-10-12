"""Declined SPT agent behavior after `pay ucp-complete` (spec #93 Track A, A7 matrix).

Every case drives the real `complete_ucp_checkout_async` against a mocked
checkout client, so the wire body, not a hand-built exception, decides the
outcome. The expected behavior does not change when E1 ships.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from booking_agent.observability import set_event_sink
from booking_agent.ucp_checkout import UcpCheckoutHttpError
from support.mock.link_cli import fake_shared_payment_token
from integration.test_flow_gates import (
    _approval_created,
    _invoke,
    _pay_request,
    _stdout_json,
    _ucp_payment_pending,
)

_HOSTED = "https://merchant.test/checkout/chk_test"
_ACTION_URL = "https://app.link.com/3ds/charge-challenge"
_ORDER_URL = "https://merchant.test/orders/ord_7"
_OLD_SR = "lsrq_old"
_NEW_CARD_CMD = "booking-agent flow pay request --payment-method-id <id>"
_SAME_SR_CMD = f"booking-agent flow pay ucp-complete --spend-request-id {_OLD_SR}"
_3DS_TEXT = (
    "This card needs extra bank verification, which isn't supported for agent "
    "bookings. Try a different card."
)
_RISK_TEXT = "This payment couldn't be processed. Try a different card."
_REPO = Path(__file__).resolve().parents[4]


def _msg(code, severity=None, content="Your card was declined."):
    m = {"type": "error", "code": code, "content": content}
    if severity is not None:
        m["severity"] = severity
    return m


def _incomplete(*messages, status="incomplete"):
    return {
        "id": "chk_test",
        "status": status,
        "continue_url": _HOSTED,
        "messages": list(messages),
    }


def _completed():
    return {
        "id": "chk_test",
        "status": "completed",
        "totals": [{"type": "total", "amount": 100}],
        "links": [],
        "booking": {"booking_id": "bk_1", "booking_status": "confirmed"},
        "order": {"id": "ord_1"},
    }


def _http(body, status=402):
    return UcpCheckoutHttpError(status, json.dumps(body) if not isinstance(body, str) else body)


@pytest.fixture
def events():
    captured: list[dict] = []
    set_event_sink(captured.append)
    return captured


@pytest.fixture
def checkout(runner, isolated_session, monkeypatch):
    """PAYMENT_PENDING UCP session with an approved spend request and a scripted checkout."""
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.session import load_session, save_session

    _ucp_payment_pending(runner)
    session = load_session()
    session.payment.spend_request_id = _OLD_SR
    session.payment.spend_approval_surfaced = True
    session.payment.spend_approval_user_surfaced = True
    session.payment.payment_method_id = "pm_old"
    session.ucp_checkout_session_id = "chk_test"
    save_session(session)

    state = {"responses": [], "posts": [], "retrieves": [], "retrieve": None}

    class FakeClient:
        async def get_checkout(self, checkout_id, **_k):
            return {
                "id": checkout_id,
                "status": "ready_for_complete",
                "continue_url": _HOSTED,
                "payment": {"instruments": [{"handler_id": "h1"}]},
                "order": {"id": "ord_7", "permalink_url": _ORDER_URL},
            }

        async def complete_checkout(self, checkout_id, body, *, idempotency_key=None):
            state["posts"].append(idempotency_key)
            nxt = state["responses"].pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

    def fake_retrieve(sr_id):
        state["retrieves"].append(sr_id)
        r = state["retrieve"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(
        "booking_agent.ucp_checkout.build_ucp_checkout_client", lambda *_a, **_k: FakeClient(),
    )
    monkeypatch.setattr(
        "booking_agent.ucp_checkout.resolve_handler_id_for_ucp_complete", lambda **_k: "h1",
    )
    monkeypatch.setattr(
        "booking_agent.ucp_checkout.build_payment_instrument_for_complete_checkout",
        lambda *_a, **_k: {"handler_id": "h1", "type": "link", "credential": {"type": "spt"}},
    )
    monkeypatch.setattr(
        "booking_agent.ucp_capabilities.validate_checkout_capabilities", lambda *_a, **_k: {},
    )
    fake_shared_payment_token(monkeypatch, "spt_x")
    monkeypatch.setattr(flow, "_poll_spend_approval_or_exit", lambda *_a, **_k: None)
    monkeypatch.setattr(flow, "retrieve_spend_request", fake_retrieve)
    return state


def _complete(runner, state, *responses):
    state["responses"] = list(responses)
    return _invoke(runner, "flow", "pay", "ucp-complete", "--spend-request-id", _OLD_SR)


def _session():
    from booking_agent_cli.session import load_session

    return load_session()


# ---------------------------------------------------------------------------
# A1 / A2 / A2.1 / A3: terminal declines ask for a new card, never continue_url
# ---------------------------------------------------------------------------

_DECLINES = {
    "legacy_http_escalation": _http(
        _incomplete(_msg("payment_declined"), status="requires_escalation"),
    ),
    "legacy_200_escalation": _incomplete(
        _msg("payment_declined"), status="requires_escalation",
    ),
    "pre_charge_recoverable": _incomplete(_msg("payment_declined", "recoverable")),
    "e1_3ds": _incomplete(_msg("payment_failed", "recoverable", _3DS_TEXT)),
    "e1_risk": _incomplete(_msg("payment_failed", "recoverable", _RISK_TEXT)),
    "e1_auth_unsupported_code": _incomplete(
        _msg("payment_authentication_unsupported", "recoverable", _3DS_TEXT),
    ),
    "e1_not_processed_code": _incomplete(
        _msg("payment_not_processed", "recoverable", _RISK_TEXT),
    ),
}


@pytest.mark.parametrize("response", list(_DECLINES.values()), ids=list(_DECLINES))
def test_decline_offers_new_card_without_continue_url(runner, checkout, events, response):
    from booking_agent_cli.link_cli import SpendRequestStatus

    checkout["retrieve"] = SpendRequestStatus(id=_OLD_SR, status="succeeded")
    body = json.loads(response.body) if isinstance(response, UcpCheckoutHttpError) else response
    message = body["messages"][0]

    r = _complete(runner, checkout, response)

    assert r.exit_code == 1, r.stdout + r.stderr
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == message["code"]
    assert out["next_command"] == _NEW_CARD_CMD
    assert _OLD_SR not in out["next_command"]
    assert "pm_old" not in out["next_command"]
    assert out["user_message"].startswith(message["content"] + "\n")
    assert "different card from your Link wallet" in out["user_message"]
    assert "different" in out["prompt"].lower()
    assert _HOSTED not in r.stdout
    assert "continue_url" not in out["data"]
    assert out["phase"] == "PAYMENT_PENDING"
    assert len(checkout["posts"]) == 1
    assert checkout["retrieves"] == [_OLD_SR]

    s = _session()
    assert s.payment.spend_request_id is None
    assert s.payment.declined_spend_request_id == _OLD_SR
    assert s.payment.declined_code == message["code"]
    assert s.payment.declined_checkout_id == "chk_test"
    assert not s.payment.spend_approval_surfaced
    assert not s.payment.spend_approval_user_surfaced
    assert s.payment.spend_action_surfaced is None
    assert s.complete_checkout_idempotency_key is None
    assert s.ucp_checkout_session_id == "chk_test"

    declined = [e for e in events if e.get("event") == "payment_declined"]
    assert len(declined) == 1
    assert declined[0]["message_code"] == message["code"]
    assert declined[0]["checkout_id"] == "chk_test"
    assert declined[0]["merchant_profile_url"]
    assert declined[0]["new_card_retry"] is False
    assert _HOSTED not in json.dumps(events)


def test_decline_skips_signal_repost(runner, checkout):
    body = _incomplete(
        _msg("payment_declined"),
        {"type": "warning", "code": "signal", "content": "retry"},
    )
    body["messages"][1]["type"] = "recoverable"

    r = _complete(runner, checkout, body, _completed())

    assert r.exit_code == 1
    assert _stdout_json(r)["next_command"] == _NEW_CARD_CMD
    assert len(checkout["posts"]) == 1


def test_payment_failed_without_recoverable_keeps_existing_handling(runner, checkout):
    r = _complete(runner, checkout, _incomplete(_msg("payment_failed")))

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "ucp_complete_failed"
    assert out["next_command"] == _SAME_SR_CMD
    assert "trying again" in out["user_message"]
    assert checkout["retrieves"] == []
    assert _session().payment.spend_request_id == _OLD_SR


_UNAVAILABLE_TEXT = (
    "The business cannot take this payment method right now, so nothing was charged. "
    "Pay another way or on the merchant's checkout page."
)
_UNAVAILABLE = {
    "acp_checkout_200": _incomplete(
        _msg("payment_failed", "unrecoverable", _UNAVAILABLE_TEXT), status="ready_for_complete",
    ),
    "http_error": _http(
        _incomplete(_msg("payment_failed", "unrecoverable", _UNAVAILABLE_TEXT)), status=422,
    ),
}


@pytest.mark.parametrize("response", list(_UNAVAILABLE.values()), ids=list(_UNAVAILABLE))
def test_payment_unrecoverable_stops_this_checkout(runner, checkout, response):
    r = _complete(runner, checkout, response)

    assert r.exit_code == 1, r.stdout + r.stderr
    out = _stdout_json(r)
    agent_message = out["data"]["error"]["message"]
    assert out["data"]["error"]["code"] == "payment_unrecoverable"
    assert out["phase"] == "FAILED"
    assert out["next_command"] is None
    assert "booking-agent flow pay ucp-complete" in out["forbidden"]
    assert "booking-agent flow pay request" in out["forbidden"]
    assert "booking-agent flow pay rebook" not in out["forbidden"]
    assert "couldn't complete this checkout" in out["user_message"]
    assert "booking wasn't finished" in out["user_message"]
    for claim in (
        "nothing was charged",
        "different card",
        "another card",
        "through Link",
        "checkout page",
        "book directly",
        "before charging",
    ):
        assert claim not in out["user_message"]
        assert claim not in agent_message
    assert _HOSTED not in r.stdout
    assert out["data"]["checkout_id"] == "chk_test"
    assert len(checkout["posts"]) == 1
    assert checkout["retrieves"] == []
    s = _session()
    assert s.flow_phase == "FAILED"
    assert s.last_error.code == "payment_unrecoverable"
    assert s.payment.status == "failed"


def test_payment_unrecoverable_is_not_retried(runner, checkout):
    _complete(runner, checkout, _UNAVAILABLE["acp_checkout_200"])

    status = _stdout_json(_invoke(runner, "flow", "status"))
    again = _invoke(runner, "flow", "pay", "ucp-complete", "--spend-request-id", _OLD_SR)

    assert "ucp-complete" not in (status["next_command"] or "")
    assert again.exit_code != 0
    assert len(checkout["posts"]) == 1


def test_non_json_transport_error_keeps_same_spend_request(runner, checkout):
    r = _complete(runner, checkout, _http("upstream unavailable", status=503))

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "ucp_complete_failed"
    assert out["next_command"] == _SAME_SR_CMD
    assert checkout["retrieves"] == []


# ---------------------------------------------------------------------------
# A5 / A5.1: post-failure requires_action never surfaces action_url
# ---------------------------------------------------------------------------

def test_post_failure_requires_action_hides_action_url(runner, checkout, events):
    from booking_agent_cli.link_cli import SpendNextAction, SpendRequestStatus

    checkout["retrieve"] = SpendRequestStatus(
        id=_OLD_SR,
        status="requires_action",
        next_action=SpendNextAction(
            type="three_d_secure",
            resolution="auto_resume",
            display_message="Your bank needs to verify this payment.",
            action_url=_ACTION_URL,
        ),
    )

    r = _complete(runner, checkout, _incomplete(_msg("payment_declined")))

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert _ACTION_URL not in r.stdout
    assert _ACTION_URL not in r.stderr
    assert out["awaiting_user"] is None
    assert out["next_command"] == _NEW_CARD_CMD
    assert "did not go through" in out["data"]["error"]["message"]
    assert checkout["retrieves"] == [_OLD_SR]
    suppressed = [e for e in events if e.get("event") == "post_failure_action_suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0]["checkout_id"] == "chk_test"
    assert suppressed[0]["spend_request_id"] == _OLD_SR
    assert _ACTION_URL not in json.dumps(events)


def test_post_failure_retrieve_error_still_offers_new_card(runner, checkout):
    checkout["retrieve"] = RuntimeError("link-cli unavailable")

    r = _complete(runner, checkout, _incomplete(_msg("payment_declined")))

    assert r.exit_code == 1
    assert _stdout_json(r)["next_command"] == _NEW_CARD_CMD
    assert checkout["retrieves"] == [_OLD_SR]


def test_evidence_keeps_the_declined_spend_request_id(runner, checkout):
    _complete(runner, checkout, _incomplete(_msg("payment_declined")))

    data = _stdout_json(_invoke(runner, "flow", "evidence"))["data"]

    assert data["spend_request_id"] is None
    assert data["declined_spend_request_id"] == _OLD_SR
    assert data["ucp_checkout_session_id"] == "chk_test"


# ---------------------------------------------------------------------------
# A2.2 - A2.5: not new-card declines
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "messages",
    [
        [_msg("complete_in_progress")],
        [_msg("checkout_already_in_progress")],
        [_msg("payment_declined"), _msg("complete_in_progress")],
    ],
    ids=["complete_in_progress", "checkout_already_in_progress", "mixed_with_decline"],
)
def test_in_progress_polls_same_checkout(runner, checkout, messages):
    r = _complete(runner, checkout, _http(_incomplete(*messages), status=409))

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] in {"complete_in_progress", "checkout_already_in_progress"}
    assert out["next_command"] == _SAME_SR_CMD
    assert "still processing your payment" in out["user_message"]
    assert "don't pay again" in out["user_message"]
    assert checkout["retrieves"] == []
    s = _session()
    assert s.payment.spend_request_id == _OLD_SR
    assert s.payment.declined_spend_request_id is None
    assert s.complete_checkout_idempotency_key == checkout["posts"][0]
    assert s.complete_checkout_idempotency_key


@pytest.mark.parametrize(
    "response",
    [
        _incomplete(_msg("payment_captured_booking_unconfirmed", "unrecoverable")),
        _incomplete(
            _msg("payment_declined"),
            _msg("payment_captured_booking_unconfirmed", "unrecoverable"),
        ),
        {
            "id": "chk_test",
            "status": "completed",
            "totals": [{"type": "total", "amount": 100}],
            "links": [],
            "booking": {"booking_id": "bk_1", "booking_status": "pending"},
            "order": {"id": "ord_1"},
        },
    ],
    ids=["message", "mixed_with_decline", "completed_booking_pending"],
)
def test_captured_unconfirmed_never_retries_or_asks_for_new_card(
    runner, checkout, events, response,
):
    r = _complete(runner, checkout, response)

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "payment_captured_booking_unconfirmed"
    assert out["next_command"] == "booking-agent flow book poll --wait"
    assert "booking-agent flow pay ucp-complete" in out["forbidden"]
    assert "booking-agent flow pay request" in out["forbidden"]
    assert "captured" in out["data"]["error"]["message"].lower()
    assert "went through" in out["user_message"]
    assert "don't pay again" in out["user_message"]
    assert "Please wait while I keep checking" in out["user_message"]
    assert "no need to contact" in out["user_message"]
    assert "before running next_command" in out["data"]["error"]["message"]
    assert checkout["retrieves"] == []
    s = _session()
    assert s.payment.spend_request_id == _OLD_SR
    assert s.payment.captured_unconfirmed
    assert s.payment.status == "paid"
    assert any(e.get("event") == "payment_captured_booking_unconfirmed" for e in events)


# ---------------------------------------------------------------------------
# Payment captured, booking unconfirmed: recovery and refusals
# ---------------------------------------------------------------------------

def _capture_without_booking(runner, checkout):
    r = _complete(
        runner, checkout,
        _incomplete(_msg("payment_captured_booking_unconfirmed", "unrecoverable")),
    )
    assert r.exit_code == 1
    assert _session().payment.captured_unconfirmed


def _booking_poll_returns(monkeypatch, outcome):
    import booking_agent_cli.commands.flow as flow
    from types import SimpleNamespace

    def fake_wait(*_a, **_k):
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(status=outcome, revision=None)

    monkeypatch.setattr(flow, "wait_for_booking_terminal", fake_wait)


def test_status_after_capture_points_at_booking_poll_not_a_new_charge(runner, checkout):
    _capture_without_booking(runner, checkout)

    out = _stdout_json(_invoke(runner, "flow", "status"))

    assert out["next_command"] == "booking-agent flow book poll --wait"
    assert "ucp-complete" not in (out["next_command"] or "")
    assert "Never retry" in out["prompt"]


@pytest.mark.parametrize(
    "args",
    [
        ("flow", "pay", "ucp-complete", "--spend-request-id", _OLD_SR),
        ("flow", "pay", "request", "--payment-method-id", "pm_x", "--context", "x" * 100),
        ("flow", "pay", "rebook", "--service-id", "svc"),
    ],
    ids=["ucp-complete", "pay-request", "rebook"],
)
def test_cli_refuses_a_second_charge_after_capture(runner, checkout, args):
    _capture_without_booking(runner, checkout)
    posts_before = len(checkout["posts"])

    r = _invoke(runner, *args)

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "payment_already_captured"
    assert out["next_command"] == "booking-agent flow book poll --wait"
    assert len(checkout["posts"]) == posts_before


def test_late_confirmation_finishes_the_booking(runner, checkout, monkeypatch):
    _capture_without_booking(runner, checkout)
    _booking_poll_returns(monkeypatch, "confirmed")

    r = _invoke(runner, "flow", "book", "poll", "--wait")

    assert r.exit_code == 0, r.stdout + r.stderr
    out = _stdout_json(r)
    assert out["phase"] == "CONFIRMED"
    assert "booking confirmed" in out["prompt"]
    s = _session()
    assert not s.payment.captured_unconfirmed
    assert s.payment.status == "paid"


def _all_json(result):
    import json as _json

    dec = _json.JSONDecoder()
    s, idx, objs = result.stdout, 0, []
    while True:
        idx = s.find("{", idx)
        if idx == -1:
            return objs
        obj, idx = dec.raw_decode(s, idx)
        objs.append(obj)


def _pay_poll_into_capture(runner, checkout):
    checkout["responses"] = [
        _incomplete(_msg("payment_captured_booking_unconfirmed", "unrecoverable")),
    ]
    return _invoke(runner, "flow", "pay", "poll")


def test_pay_poll_waits_through_capture_and_reports_the_confirmed_booking_once(
    runner, checkout, monkeypatch,
):
    """The background poll is the one place the outcome arrives; no false failure first."""
    _booking_poll_returns(monkeypatch, "confirmed")

    r = _pay_poll_into_capture(runner, checkout)

    assert r.exit_code == 0, r.stdout + r.stderr
    outputs = _all_json(r)
    waiting, final = outputs[0], outputs[-1]
    assert waiting["data"]["status"] == "waiting_for_business_confirmation"
    assert waiting["data"].get("error") is None
    assert "went through" in waiting["user_message"]
    assert "don't pay again" in waiting["user_message"]
    assert final["phase"] == "CONFIRMED"
    assert "booking confirmed" in final["prompt"]
    assert sum("booking confirmed" in (o.get("prompt") or "") for o in outputs) == 1
    assert not any(
        (o.get("data") or {}).get("error", {}).get("code") == "payment_captured_booking_unconfirmed"
        for o in outputs
    )
    assert sum("went through" in (o.get("user_message") or "") for o in outputs) == 1
    s = _session()
    assert s.flow_phase == "CONFIRMED"
    assert not s.payment.captured_unconfirmed
    assert len(checkout["posts"]) == 1


def test_pay_poll_capture_that_never_confirms_ends_with_the_business_handoff(
    runner, checkout, monkeypatch,
):
    from booking_agent_cli.booking_poll import BookingPollTimeout

    _booking_poll_returns(monkeypatch, BookingPollTimeout("still pending"))

    r = _pay_poll_into_capture(runner, checkout)

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "payment_captured_booking_unconfirmed"
    assert "refund" in out["user_message"]
    assert "booking-agent flow pay request" in out["forbidden"]
    assert len(checkout["posts"]) == 1


def test_pay_poll_capture_then_cancel_asks_for_refund(runner, checkout, monkeypatch):
    _booking_poll_returns(monkeypatch, "canceled")

    r = _pay_poll_into_capture(runner, checkout)

    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "booking_canceled_after_payment"


def test_still_unconfirmed_hands_the_user_to_the_business(runner, checkout, monkeypatch):
    from booking_agent_cli.booking_poll import BookingPollTimeout

    _capture_without_booking(runner, checkout)
    _booking_poll_returns(monkeypatch, BookingPollTimeout("still pending"))

    r = _invoke(runner, "flow", "book", "poll", "--wait")

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "payment_captured_booking_unconfirmed"
    msg = out["user_message"]
    assert "went through" in msg and "don't pay again" in msg
    assert "refund" in msg
    contact_urls = [u["url"] for u in out["data"]["contact_urls"]]
    assert _ORDER_URL not in r.stdout
    assert contact_urls[0] in msg
    assert "Order reference: ord_7" in msg
    assert "dispute" in msg
    assert _HOSTED not in r.stdout
    assert all(u["type"] != "checkout" for u in out["data"]["contact_urls"])
    assert "booking-agent flow evidence" in out["data"]["error"]["message"]
    assert out["next_command"] == "booking-agent flow book poll --wait"
    assert _session().payment.captured_unconfirmed


def test_canceled_after_capture_asks_for_refund(runner, checkout, monkeypatch):
    _capture_without_booking(runner, checkout)
    _booking_poll_returns(monkeypatch, "canceled")

    r = _invoke(runner, "flow", "book", "poll", "--wait")

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "booking_canceled_after_payment"
    assert "canceled this booking after your payment went through" in out["user_message"]
    assert "refund" in out["user_message"]
    assert _HOSTED not in r.stdout
    assert out["next_command"] is None
    assert "booking-agent flow pay request" in out["forbidden"]


def test_mandatory_acceptance_still_presents_continue_url(runner, checkout):
    r = _complete(
        runner,
        checkout,
        _incomplete(_msg("mandatory_acceptance_pending", "requires_buyer_review")),
    )

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "mandatory_acceptance_pending"
    assert out["data"]["continue_url"] == _HOSTED
    assert _HOSTED in out["user_message"]
    assert "Let me know once you've done that" in out["user_message"]
    assert out["next_command"] == _SAME_SR_CMD
    assert checkout["retrieves"] == []
    assert _session().payment.spend_request_id == _OLD_SR


def test_completed_checkout_is_unchanged(runner, checkout, events):
    r = _complete(runner, checkout, _completed())

    assert r.exit_code == 0, r.stdout + r.stderr
    out = _stdout_json(r)
    assert out["phase"] == "CONFIRMED"
    assert out["next_command"] == "booking-agent flow book poll --wait"
    assert checkout["retrieves"] == []
    assert not any(e.get("event") == "declined_spt_retry_succeeded" for e in events)


# ---------------------------------------------------------------------------
# Recovery: a new spend request replaces the declined one (A7.1 logging)
# ---------------------------------------------------------------------------

def test_new_card_after_decline_books_and_logs_retry_success(
    runner, checkout, events, monkeypatch,
):
    from booking_agent_cli.session import save_session

    r = _complete(runner, checkout, _incomplete(_msg("payment_declined")))
    assert r.exit_code == 1
    first_key = checkout["posts"][0]

    r_new, calls = _pay_request(runner, monkeypatch, _approval_created("lsrq_new"))
    assert r_new.exit_code == 0, r_new.stdout + r_new.stderr
    assert len(calls) == 1
    s = _session()
    assert s.payment.spend_request_id == "lsrq_new"
    s.payment.spend_approval_user_surfaced = True
    save_session(s)

    checkout["responses"] = [_completed()]
    r_ok = _invoke(runner, "flow", "pay", "ucp-complete", "--spend-request-id", "lsrq_new")

    assert r_ok.exit_code == 0, r_ok.stdout + r_ok.stderr
    assert _stdout_json(r_ok)["phase"] == "CONFIRMED"
    assert checkout["posts"][1] != first_key
    retried = [e for e in events if e.get("event") == "declined_spt_retry_succeeded"]
    assert len(retried) == 1
    assert retried[0]["declined_spend_request_id"] == _OLD_SR
    assert retried[0]["spend_request_id"] == "lsrq_new"
    assert retried[0]["message_code"] == "payment_declined"
    assert retried[0]["merchant_profile_url"]
    assert _session().payment.declined_spend_request_id is None


def test_second_decline_is_logged_as_failed_new_card_retry(
    runner, checkout, events, monkeypatch,
):
    from booking_agent_cli.session import save_session

    _complete(runner, checkout, _incomplete(_msg("payment_declined")))
    _pay_request(runner, monkeypatch, _approval_created("lsrq_new"))
    s = _session()
    s.payment.spend_approval_user_surfaced = True
    save_session(s)

    checkout["responses"] = [_incomplete(_msg("payment_failed", "recoverable", _3DS_TEXT))]
    r = _invoke(runner, "flow", "pay", "ucp-complete", "--spend-request-id", "lsrq_new")

    assert r.exit_code == 1
    assert _stdout_json(r)["next_command"] == _NEW_CARD_CMD
    declined = [e for e in events if e.get("event") == "payment_declined"]
    assert [e["new_card_retry"] for e in declined] == [False, True]
    assert _session().payment.declined_spend_request_id == "lsrq_new"


# ---------------------------------------------------------------------------
# A6: the base skill carries the matrix the CLI emits; the superagent inherits it
# ---------------------------------------------------------------------------

_MATRIX_TERMS = [
    "payment_declined",
    "payment_failed",
    "payment_authentication_unsupported",
    "payment_not_processed",
    "complete_in_progress",
    "checkout_already_in_progress",
    "payment_captured_booking_unconfirmed",
    "booking_canceled_after_payment",
    "payment_already_captured",
    "mandatory_acceptance_pending",
    "payment_unrecoverable",
    "post_failure_action_suppressed",
    "booking-agent flow evidence",
    _NEW_CARD_CMD,
]


def _skill(name):
    return (_REPO / "skills" / name / "SKILL.md").read_text()


def test_base_skill_states_the_decline_matrix():
    missing = [t for t in _MATRIX_TERMS if t not in _skill("booking-agent")]
    assert not missing, f"booking-agent SKILL.md is missing {missing}"


def test_superagent_inherits_the_matrix_without_restating_it():
    text = _skill("base44-booking-superagent")
    assert "../booking-agent/SKILL.md" in text
    assert "error recovery" in text
    restated = [t for t in _MATRIX_TERMS if t in text]
    assert not restated, f"base44-booking-superagent restates base rules: {restated}"


def test_base_skill_keeps_every_discover_result_in_registry_order():
    text = _skill("booking-agent")
    assert "Never omit, filter, or reorder results" in text
    assert "less-direct" in text
    assert "alternative with the specific mismatch" in text
    assert "`channel.type: at_business_location` when the user needs a home visit" in text
    assert "compute your own relevance score" in text


def test_base_skill_forbids_repetition_fixed_waits_and_agent_written_context():
    text = _skill("booking-agent")
    assert "Send the opening message **once per booking**" in text
    assert "report only its **final** outcome" in text
    assert "Never run `sleep`, a fixed wait" in text
    assert "Never write or pass that note yourself" in text
    assert "--context '" not in text and '--context "' not in text
    for gate in (
        "--less-direct", "--mismatch-confirmed", "--details-confirmed",
        "--timezone-confirmed", "--allow-duplicate",
    ):
        assert gate in text
    for code in (
        "service_details_changed", "timezone_confirmation_required",
        "duplicate_booking_confirmation", "intent_mismatch_confirmation",
        "waiting_for_business_confirmation", "discovery_mismatches",
    ):
        assert code in text


def test_base_skill_uses_focused_calendar_reconnect_recovery():
    text = _skill("booking-agent")
    assert "awaiting_user: calendar_reconnect" in text
    assert "Do **not** ask the three\n  calendar questions again" in text
    assert "booking-agent flow calendar skip\n  --session-only" in text
    assert "calendar_checking_unavailable" in text
