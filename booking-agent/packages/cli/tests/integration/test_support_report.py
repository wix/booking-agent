"""Consented support report.

The CLI freezes a note on the session after an unrecoverable failure, asks the
user once, and posts the stored bytes only after `send --user-confirmed`.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import sys
from pathlib import Path

import httpx
import jsonschema
import pytest

import booking_agent_cli.support_report as sr
from booking_agent_cli.session import ActivityEntry, Session
from booking_agent_cli.support_bundle_schema import SUPPORT_BUNDLE_SCHEMA as _SCHEMA
from booking_agent_cli.support_bundle_schema import validate_bundle
from integration.test_declined_spt import (  # noqa: F401  (fixtures)
    _OLD_SR,
    _UNAVAILABLE,
    _complete,
    _incomplete,
    _msg,
    _session,
    checkout,
    events,
)
from integration.test_flow_gates import _invoke, _stdout_json

_REPO = Path(__file__).resolve().parents[4]
_SKILL = _REPO / "skills" / "booking-agent" / "SKILL.md"
_VALIDATOR = jsonschema.Draft202012Validator(_SCHEMA)

_BUYER_NAME = "Ada Lovelace"
_BUYER_EMAIL = "ada.lovelace@example.com"
_BUYER_PHONE = "+1 415 555 0134"
_STREET = "221B Baker Street"
_CREDENTIAL = "bsc_supersecretcredential"
_BEARER = "Bearer eyJhbGciOiJIUzI1NiJ9.leaky"
_CONTINUE = "https://merchant.test/checkout/chk_test?token=leak"
_PM = "pm_secretcard"
_LAT, _LNG = 51.523767, -0.158555
_NOTES = "ring twice, gate code QZXK"
_RECIPIENT = "attacker@evil.test"


def _validate(text: str) -> dict:
    data = json.loads(text)
    errors = [f"{list(e.path)}: {e.message}" for e in _VALIDATOR.iter_errors(data)]
    assert not errors, errors
    assert validate_bundle(data) == []
    return data


def _stored() -> dict:
    raw = _session().support_report_bytes
    assert raw, "no stored note"
    return _validate(raw)


@pytest.fixture
def posts(monkeypatch):
    calls: list = []

    def fake_post(url, body):
        calls.append((url, body))
        return None

    monkeypatch.setattr(sr, "_post_report", fake_post)
    return calls


def _rebook_fails(runner, monkeypatch, exc: Exception | None = None, before=None):
    """`pay rebook` with a raising booking create: `rebook_failed`, no next_command."""
    import booking_agent_cli.commands.flow as flow

    def boom(*_a, **_k):
        if before is not None:
            before()
        raise exc or RuntimeError("upstream exploded")

    monkeypatch.setattr(flow, "create_booking_sync", boom)
    return _invoke(runner, "flow", "pay", "rebook", "--service-id", "svc_1")


def _offer(runner, monkeypatch, **kw) -> dict:
    r = _rebook_fails(runner, monkeypatch, **kw)
    assert r.exit_code == 1, r.stdout + r.stderr
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "rebook_failed"
    assert out.get("support_report_offered") is True, out
    return out


def _http_failure(host: str, response: httpx.Response, path: str = "/checkout-sessions/chk_test/complete"):
    """One request through the real traced client, so the always-on hook records it."""
    from booking_agent.http_trace import traced_async_client

    async def go():
        async with traced_async_client(transport=httpx.MockTransport(lambda _r: response)) as c:
            await c.post(f"https://{host}{path}", json={"x": 1})

    asyncio.run(go())


# ---------------------------------------------------------------------------
# Consent and commands
# ---------------------------------------------------------------------------

def test_offer_points_at_the_consent_step_and_keeps_the_recovery(runner, checkout, monkeypatch):
    out = _offer(runner, monkeypatch)

    assert out["next_command"] == sr.SUPPORT_REPORT_COMMAND
    assert out["awaiting_user"] is None
    s = _session()
    assert s.support_report_bytes
    assert s.support_report_recovery_command is None
    assert not s.support_report_asked


def test_consent_is_asked_once_with_the_exact_words(runner, checkout, monkeypatch, posts):
    _offer(runner, monkeypatch)

    first = _stdout_json(_invoke(runner, "flow", "support-report"))
    second = _stdout_json(_invoke(runner, "flow", "support-report"))

    assert first["awaiting_user"] == sr.AWAITING_SUPPORT_REPORT_CONSENT
    assert first["user_message"] == sr.CONSENT_MESSAGE
    assert first["next_command"] == sr.SEND_COMMAND
    assert first["data"]["report"]["error"]["code"] == "rebook_failed"
    assert second["awaiting_user"] == sr.AWAITING_SUPPORT_REPORT_CONSENT
    assert second.get("user_message") is None
    assert "already asked" in second["prompt"]
    assert posts == []


def test_send_without_user_confirmed_makes_no_http_call(runner, checkout, monkeypatch, posts):
    _offer(runner, monkeypatch)

    r = _invoke(runner, "flow", "support-report", "send")

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "support_report_not_confirmed"
    assert out.get("support_report_offered") is None
    assert posts == []
    assert _session().support_report_bytes


def test_send_posts_the_stored_bytes_once_and_restores_the_flow(runner, checkout, monkeypatch, posts):
    _offer(runner, monkeypatch)
    stored = _session().support_report_bytes
    _invoke(runner, "flow", "support-report")

    r = _invoke(runner, "flow", "support-report", "send", "--user-confirmed")
    again = _invoke(runner, "flow", "support-report", "send", "--user-confirmed")

    assert r.exit_code == 0, r.stdout + r.stderr
    out = _stdout_json(r)
    assert out["user_message"] == sr.SENT_MESSAGE
    assert out["data"]["sent"] is True
    assert out["next_command"] and "support-report" not in out["next_command"]
    assert posts == [(sr.DEFAULT_SUPPORT_INGEST_URL, stored.encode("utf-8"))]
    assert _stdout_json(again)["data"] == {"sent": False, "pending": False}
    s = _session()
    assert s.support_report_bytes is None
    assert not s.support_report_asked


def test_decline_drops_the_note_and_suppresses_later_offers(runner, checkout, monkeypatch, posts):
    _offer(runner, monkeypatch)

    out = _stdout_json(_invoke(runner, "flow", "support-report", "decline"))
    later = _stdout_json(_rebook_fails(runner, monkeypatch))

    assert out["data"]["declined"] is True
    assert posts == []
    assert later["data"]["error"]["code"] == "rebook_failed"
    assert later.get("support_report_offered") is None
    assert later["next_command"] is None
    s = _session()
    assert s.support_report_declined
    assert s.support_report_bytes is None


def test_status_returns_the_gate_while_a_note_is_pending(runner, checkout, monkeypatch):
    _offer(runner, monkeypatch)

    out = _stdout_json(_invoke(runner, "flow", "status"))

    assert out["awaiting_user"] == sr.AWAITING_SUPPORT_REPORT_CONSENT
    assert out["next_command"] == sr.SEND_COMMAND


def test_status_gate_wins_over_the_captured_payment_branch(runner, checkout):
    from booking_agent_cli.session import load_session, save_session

    r = _complete(
        runner, checkout, _incomplete(_msg("payment_captured_booking_unconfirmed", "unrecoverable")),
    )
    assert r.exit_code == 1
    s = load_session()
    assert s.payment.captured_unconfirmed
    assert sr.maybe_offer(s, "payment_captured_booking_unconfirmed", None, s.flow_phase)
    save_session(s, write_support_report=True)

    out = _stdout_json(_invoke(runner, "flow", "status"))

    assert out["awaiting_user"] == sr.AWAITING_SUPPORT_REPORT_CONSENT
    assert json.loads(load_session().support_report_bytes)["error"]["offer_reason"] == "captured_no_next"


def test_snapshot_survives_later_saves(runner, checkout, monkeypatch):
    from booking_agent_cli.session import load_session, save_session

    stale = load_session()
    _offer(runner, monkeypatch)
    stored = _session().support_report_bytes

    stale.slot_user_confirmed = True
    save_session(stale)
    _invoke(runner, "flow", "status")
    _invoke(runner, "flow", "evidence")

    s = _session()
    assert s.support_report_bytes == stored
    assert s.slot_user_confirmed


@pytest.mark.parametrize(
    "stored",
    [
        "{not json",
        json.dumps({**json.loads(sr._minimal_bundle("x_failed", [])), "recipient": _RECIPIENT}),
    ],
    ids=["not_json", "fails_schema"],
)
def test_corrupt_snapshot_is_unavailable(runner, checkout, monkeypatch, posts, stored):
    from booking_agent_cli.session import load_session, save_session

    s = load_session()
    s.support_report_bytes = stored
    save_session(s, write_support_report=True)

    r = _invoke(runner, "flow", "support-report", "send", "--user-confirmed")

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "support_report_unavailable"
    assert out.get("support_report_offered") is None
    assert posts == []
    assert _session().support_report_bytes is None


@pytest.mark.parametrize(
    "outcome",
    [httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), "http_500", "rate_limited"],
    ids=["timeout", "connection_error", "http_500", "rate_limited"],
)
def test_send_failure_keeps_the_note_and_makes_no_new_offer(runner, checkout, monkeypatch, outcome):
    calls = []

    def fake_post(url, body):
        calls.append(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sr, "_post_report", fake_post)
    _offer(runner, monkeypatch)
    stored = _session().support_report_bytes

    r = _invoke(runner, "flow", "support-report", "send", "--user-confirmed")
    later = _stdout_json(_rebook_fails(runner, monkeypatch))
    status = _stdout_json(_invoke(runner, "flow", "status"))

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "support_report_send_failed"
    assert out["user_message"] == sr.SEND_FAILED_MESSAGE
    assert out.get("support_report_offered") is None
    assert "support-report" not in (out["next_command"] or "")
    assert calls == [sr.DEFAULT_SUPPORT_INGEST_URL]
    assert _session().support_report_bytes == stored
    assert later.get("support_report_offered") is None
    assert status.get("awaiting_user") != sr.AWAITING_SUPPORT_REPORT_CONSENT


def test_user_asked_retry_uses_the_retry_wording_and_a_second_failure_drops_it(
    runner, checkout, monkeypatch,
):
    monkeypatch.setattr(sr, "_post_report", lambda _u, _b: "http_503")
    _offer(runner, monkeypatch)
    _invoke(runner, "flow", "support-report")
    _invoke(runner, "flow", "support-report", "send", "--user-confirmed")

    retry = _stdout_json(_invoke(runner, "flow", "support-report"))
    _invoke(runner, "flow", "support-report", "send", "--user-confirmed")

    assert retry["user_message"] == sr.RETRY_CONSENT_MESSAGE
    assert _session().support_report_bytes is None


# ---------------------------------------------------------------------------
# Offer policy
# ---------------------------------------------------------------------------

def _log(*entries) -> Session:
    s = Session()
    s.activity_log = [
        ActivityEntry(at="2026-10-11T10:00:00+00:00", kind=k, text=t, code=c) for k, t, c in entries
    ]
    return s


@pytest.mark.parametrize(
    "code,next_command",
    [
        ("payment_declined", "booking-agent flow pay request --payment-method-id <id> --context '<x>'"),
        ("slot_not_selected", "booking-agent flow slots list"),
        ("link_auth_timeout", "booking-agent flow link-auth connect"),
        ("ucp_native_required", None),
        ("payment_unrecoverable", None),
        ("ucp_complete_failed", "booking-agent flow pay ucp-complete --spend-request-id <id>"),
    ],
)
def test_first_expected_code_does_not_offer(code, next_command):
    assert sr.offer_reason(_log(("command", "booking-agent flow x", None)), code, next_command) is None


def test_second_expected_code_after_another_command_offers():
    s = _log(
        ("command", "booking-agent flow pay ucp-complete --spend-request-id lsrq_1", None),
        ("error", "declined", "payment_declined"),
        ("command", "booking-agent flow pay ucp-complete --spend-request-id lsrq_2", None),
    )
    assert sr.offer_reason(s, "payment_declined", "booking-agent flow pay request") == "repeated"


def test_still_working_codes_and_poll_loops_never_count_as_repeats():
    s = _log(
        ("error", "x", "complete_in_progress"),
        ("command", "booking-agent flow pay ucp-complete", None),
        ("error", "x", "payment_captured_booking_unconfirmed"),
        ("command", "booking-agent flow book poll --wait", None),
    )
    assert sr.offer_reason(s, "complete_in_progress", "booking-agent flow pay ucp-complete") is None
    assert sr.offer_reason(
        s, "payment_captured_booking_unconfirmed", "booking-agent flow book poll --wait", "flow book poll",
    ) is None


def test_unknown_code_offers_only_without_a_recovery_command():
    s = Session()
    assert sr.offer_reason(s, "brand_new_failure", None) == "unknown_code"
    assert sr.offer_reason(s, "checkout_url_expired", "booking-agent flow pay rebook --service-id <id>") is None
    assert sr.offer_reason(s, "internal_error", "booking-agent flow status") == "internal_error"
    assert sr.offer_reason(s, "payment_captured_booking_unconfirmed", None) == "captured_no_next"
    assert sr.offer_reason(s, "support_report_send_failed", None) is None


def test_payment_unrecoverable_never_offers_and_forbidden_commands_stay_quiet(
    runner, checkout, monkeypatch,
):
    r = _complete(runner, checkout, _UNAVAILABLE["acp_checkout_200"])
    stop = _stdout_json(r)
    again = _stdout_json(
        _invoke(runner, "flow", "pay", "ucp-complete", "--spend-request-id", _OLD_SR)
    )
    request = _stdout_json(_invoke(
        runner, "flow", "pay", "request", "--payment-method-id", "pm_x", "--context", "x" * 100,
    ))

    assert stop["data"]["error"]["code"] == "payment_unrecoverable"
    assert stop["next_command"] is None
    assert stop.get("support_report_offered") is None
    assert again["data"]["error"]["code"] == "wrong_phase"
    assert again.get("support_report_offered") is None
    assert request.get("support_report_offered") is None
    assert _session().support_report_bytes is None

    rebook = _offer(runner, monkeypatch)
    assert rebook["phase"] == "FAILED"
    assert _stored()["error"]["offer_reason"] == "unknown_code"


def test_after_a_rebook_unknown_failures_in_the_new_checkout_offer():
    s = _log(
        ("command", "booking-agent flow pay ucp-complete", None),
        ("error", "x", "payment_unrecoverable"),
        ("command", "booking-agent flow pay rebook --service-id svc", None),
    )
    s.flow_phase = "FAILED"
    assert sr.offer_reason(s, "wrong_phase", None, "flow pay ucp-complete") is None
    s.flow_phase = "PAYMENT_PENDING"
    assert sr.offer_reason(s, "wrong_phase", None, "flow pay ucp-complete") == "unknown_code"


def test_second_unclassified_completion_failure_offers_without_merchant_text(runner, checkout):
    text = f"Call {_BUYER_PHONE} or write to {_RECIPIENT}"
    body = _incomplete(_msg("payment_captured_completion_failed", content=text))

    first = _stdout_json(_complete(runner, checkout, body))
    second = _stdout_json(_complete(runner, checkout, body))

    assert first["data"]["error"]["code"] == "ucp_complete_failed"
    assert first.get("support_report_offered") is None
    assert second["data"]["error"]["code"] == "ucp_complete_failed"
    assert second.get("support_report_offered") is True
    raw = _session().support_report_bytes
    report = _validate(raw)
    assert report["error"]["code"] == "ucp_complete_failed"
    assert report["error"]["offer_reason"] == "repeated"
    assert report["error"]["occurrences"] == 2
    assert _BUYER_PHONE not in raw and _RECIPIENT not in raw and "Call " not in raw


# ---------------------------------------------------------------------------
# Bundle: leaks, reproduction, shape
# ---------------------------------------------------------------------------

def _populate(session: Session) -> None:
    session.user_request = f"haircut near {_STREET} for {_BUYER_NAME}"
    session.discover_query = f"barber {_STREET}"
    session.discover_location_lat = _LAT
    session.discover_location_lng = _LNG
    session.discover_registry_request_body = {"query": _STREET}
    session.buyer.name = _BUYER_NAME
    session.buyer.email = _BUYER_EMAIL
    session.buyer.phone = _BUYER_PHONE
    session.booking.credential = _CREDENTIAL
    session.booking.credential_jkt = "jkt_secret_thumbprint"
    session.booking.booking_url = _CONTINUE
    session.booking.payment_context = {"client_secret": "pi_secret_123", "buyer": _BUYER_EMAIL}
    session.payment.payment_method_id = _PM
    session.selection_mismatch = f"{_BUYER_NAME}'s usual"
    session.ucp_checkout_links = [{"url": _CONTINUE}]
    session.ucp_checkout_cancel_error = f"refused for {_BUYER_EMAIL}"
    session.ucp_checkout_messages = [
        {"type": "error", "code": "payment_declined", "severity": "recoverable",
         "content": f"Card for {_BUYER_NAME} at {_STREET} declined"},
    ]
    address = {"line1": _STREET, "city": "London", "postal_code": "NW1 6XE", "country": "GB",
               "coordinates": {"latitude": _LAT, "longitude": _LNG}}
    session.search_area = {"label": f"{_STREET}, London", "lat": _LAT, "lng": _LNG, "confirmed": True,
                           "address": address}
    session.pending_area = {"label": _STREET, "lat": _LAT, "lng": _LNG}
    session.service_address = address
    session.service_address_label = f"{_STREET}, London NW1 6XE"
    session.area_resume_command = f"booking-agent flow business use --notes '{_NOTES}'"
    session.booking_notes = _NOTES
    session.activity_log.extend([
        ActivityEntry(
            at="2026-10-11T10:00:00+00:00", kind="command",
            text=f"booking-agent flow init --user-request '{session.user_request}'",
        ),
        ActivityEntry(
            at="2026-10-11T10:01:00+00:00", kind="command",
            text=f"booking-agent flow pay request --payment-method-id {_PM} --context 'for {_BUYER_NAME}'",
        ),
        ActivityEntry(
            at="2026-10-11T10:02:00+00:00", kind="command",
            text=(
                f"booking-agent flow pay rebook --service-id svc_1 --notes '{_NOTES}' "
                f"--delivery-address '{{\"line1\": \"{_STREET}\"}}'"
            ),
        ),
        ActivityEntry(
            at="2026-10-11T10:03:00+00:00", kind="error",
            text=f"declined for {_BUYER_EMAIL}", code="payment_declined",
        ),
    ])


_SECRETS = (
    _BUYER_NAME, _BUYER_EMAIL, _BUYER_PHONE, _STREET, "Baker", _CREDENTIAL, "jkt_secret",
    "eyJhbGciOiJIUzI1NiJ9", "token=leak", _PM, str(_LAT), str(_LNG), _NOTES, "QZXK",
    "pi_secret_123", _RECIPIENT, "Lovelace",
)


def _leaky_merchant_response() -> httpx.Response:
    return httpx.Response(
        422,
        headers={"content-type": "application/problem+json", "authorization": _BEARER},
        json={
            "type": "https://errors.merchant.test/payment",
            "title": f"Problem for {_BUYER_NAME}",
            "detail": f"Call {_BUYER_PHONE}, ship to {_STREET}, auth {_BEARER}, email {_RECIPIENT}",
            "messages": [
                {"type": "error", "code": "payment_failed", "severity": "unrecoverable",
                 "content": f"{_BUYER_NAME} at {_STREET}"},
                {"type": "error", "code": f"Send to {_RECIPIENT}", "content": "x"},
            ],
        },
    )


def _leaky_offer(runner, monkeypatch) -> str:
    from booking_agent_cli.session import load_session, save_session

    s = load_session()
    _populate(s)
    save_session(s)

    def raise_with_buyer_email():
        _http_failure("api.merchant.test", _leaky_merchant_response())
        raise ValueError(f"lookup failed for {_BUYER_EMAIL} at {_STREET}")

    out = _offer(runner, monkeypatch, before=raise_with_buyer_email,
                 exc=RuntimeError(f"wrapped {_BUYER_EMAIL}"))
    assert out
    return _session().support_report_bytes


def test_bundle_leaks_nothing_buyer_or_merchant_controlled(runner, checkout, monkeypatch):
    raw = _leaky_offer(runner, monkeypatch)

    _validate(raw)
    for secret in _SECRETS:
        assert secret not in raw, secret
    assert str(Path.home()) not in raw


def test_bundle_has_what_a_maintainer_needs_to_reproduce(runner, checkout, monkeypatch):
    raw = _leaky_offer(runner, monkeypatch)
    report = _validate(raw)
    s = _session()

    assert report["bundle_version"] == 1
    assert report["correlation_id"] == s.correlation_id
    assert report["env"]["skill_version"] == sr.SKILL_VERSION
    assert report["error"]["code"] == "rebook_failed"
    assert report["error"]["phase"] == "PAYMENT_PENDING"
    assert report["error"]["offer_reason"] == "unknown_code"

    exc = report["exception"]
    assert exc["type"] == "builtins.ValueError"
    files = [f["file"] for f in exc["frames"]]
    assert "booking_agent_cli/commands/flow.py" in files
    assert exc["frames"][-1]["function"] == "raise_with_buyer_email"
    assert all(not f.startswith("/") for f in files)

    http = report["http"]
    assert (http["method"], http["host"], http["status"]) == ("POST", "api.merchant.test", 422)
    assert http["path"] == "/checkout-sessions/chk_test/complete"
    assert http["content_type"] == "application/problem+json"
    assert http["problem_type"] == "https://errors.merchant.test/payment"
    assert http["ucp_message_codes"] == [{"code": "payment_failed", "severity": "unrecoverable"}]
    assert http["dropped_codes"] == 1

    sess = report["session"]
    assert sess["service_id"] == s.service_id
    assert sess["slot_id"] == s.slot_id
    assert sess["booking"]["id"] == s.booking.id
    assert sess["booking"]["scoped_auth_present"] is True
    assert sess["ucp_checkout_session_id"] == "chk_test"
    assert sess["payment"]["spend_request_id"] == _OLD_SR
    assert sess["slots_presented"] and sess["slots_presented"][0]["start"]
    assert sess["calendar_gate_completed"] is True
    assert sess["link_auth_gate_completed"] is True
    assert sess["search_area_confirmed"] is True
    assert sess["service_address_set"] is True
    assert sess["ucp_checkout_messages"] == [{"code": "payment_declined", "severity": "recoverable"}]

    commands = [a["command"] for a in report["activity"] if a["kind"] == "command"]
    assert "booking-agent flow init --user-request" in commands
    assert "booking-agent flow pay request --payment-method-id --context" in commands
    assert "booking-agent flow pay rebook --service-id --notes --delivery-address" in commands
    errors = [a for a in report["activity"] if a["kind"] == "error"]
    assert errors and all(set(a) == {"at", "kind", "command", "code"} for a in errors)


def test_every_session_field_is_classified(monkeypatch):
    assert sr.unclassified_fields() == []

    Extended = dataclasses.make_dataclass(
        "Extended", [("brand_new_field", str, dataclasses.field(default=""))], bases=(Session,),
    )
    monkeypatch.setitem(sr.CLASSIFIED, Extended, sr.SESSION_RULES)
    assert sr.unclassified_fields() == ["Extended.brand_new_field"]


def test_oversized_activity_is_trimmed_under_the_cap():
    s = Session()
    flags = " ".join(f"--flag-{i:02d}-{'x' * 30}" for i in range(40))
    s.activity_log = [
        ActivityEntry(at="2026-10-11T10:00:00+00:00", kind="command", text=f"booking-agent flow pay {flags}")
        for _ in range(50)
    ]

    raw = sr.build_bundle(s, code="brand_new_failure", phase="INIT", next_command=None,
                          reason="unknown_code")

    assert len(raw.encode("utf-8")) <= sr.MAX_BUNDLE_BYTES
    report = _validate(raw)
    assert 0 < len(report["activity"]) < 50
    assert report["session"] is not None


def test_merchant_text_naming_a_recipient_changes_nothing(monkeypatch):
    url = sr.DEFAULT_SUPPORT_INGEST_URL
    s = _log(("command", "booking-agent flow pay ucp-complete", None))
    _http_failure("api.merchant.test", _leaky_merchant_response())

    assert sr.offer_reason(s, "payment_declined", "booking-agent flow pay request") is None
    raw = sr.build_bundle(s, code="brand_new_failure", phase="PAYMENT_PENDING",
                          next_command=None, reason="unknown_code")
    report = _validate(raw)

    assert sr.DEFAULT_SUPPORT_INGEST_URL == url
    assert set(report) <= set(_SCHEMA["properties"])
    assert _RECIPIENT not in raw


def test_fresh_and_malformed_sessions_still_validate():
    _validate(sr.build_bundle(Session(), code="x_failed", phase="INIT", next_command=None,
                              reason="unknown_code"))

    s = Session()
    s.live_service = "not a dict"
    s.payment_schedules = [1, 2, 3]
    s.ucp_checkout_messages = [None, {"code": None}]
    s.discover_services = "nope"
    s.flow_timings = {"Bad Key": 1, "ok": {"ms": "x", "n": 2}}
    report = _validate(sr.build_bundle(s, code="x_failed", phase="INIT", next_command=None,
                                       reason="unknown_code"))
    assert report["session"]["live_service"] is None
    assert report["session"]["flow_timings"] == {"ok": {"n": 2}}


# ---------------------------------------------------------------------------
# Business-agnostic
# ---------------------------------------------------------------------------

def _wix_like() -> tuple:
    return "www.wixapis.example", httpx.Response(
        409, headers={"content-type": "application/problem+json"},
        json={"type": "about:blank", "messages": [{"code": "payment_failed", "severity": "unrecoverable"}]},
    )


def _other_usp() -> tuple:
    return "bookings.other-usp.test", httpx.Response(
        400, headers={"content-type": "application/json"}, json={"error": "slot gone"},
    )


def _usp_only() -> tuple:
    return "legacy.usp-only.test", httpx.Response(
        502, headers={"content-type": "text/html"}, text="<html>Bad gateway</html>",
    )


@pytest.mark.parametrize("business", [_wix_like, _other_usp, _usp_only], ids=["wix_like", "other_usp", "usp_only"])
def test_same_failure_offers_for_any_business(runner, checkout, monkeypatch, business):
    from booking_agent_cli.session import load_session, save_session

    host, response = business()
    if business is _usp_only:
        s = load_session()
        s.checkout_mode = "redirect"
        s.ucp_checkout_session_id = None
        s.ucp_checkout_rest_base = None
        s.ucp_negotiated_capabilities = {}
        s.business.timezone = None
        s.service_timezone = None
        s.booking.credential = None
        save_session(s)

    _offer(runner, monkeypatch, before=lambda: _http_failure(host, response))
    report = _stored()

    assert report["http"]["host"] == host
    assert report["error"]["offer_reason"] == "unknown_code"
    if business is _wix_like:
        assert report["http"]["ucp_message_codes"] == [{"code": "payment_failed", "severity": "unrecoverable"}]
    else:
        assert report["http"]["ucp_message_codes"] == []
        assert report["http"]["problem_type"] is None
    if business is _usp_only:
        assert report["http"]["content_type"] == "text/html"
        assert report["session"]["business"]["ucp_checkout"] is False
        assert report["session"]["booking"]["scoped_auth_present"] is False


def test_no_code_path_branches_on_a_vendor():
    source = Path(sr.__file__).read_text(encoding="utf-8").lower()
    assert "wix" not in source


# ---------------------------------------------------------------------------
# Fail-safe
# ---------------------------------------------------------------------------

def test_a_raising_section_becomes_null_with_a_build_error(monkeypatch):
    def broken(_session):
        raise KeyError("boom")

    monkeypatch.setattr(sr, "_activity", broken)
    report = _validate(sr.build_bundle(Session(), code="x_failed", phase="INIT",
                                       next_command=None, reason="unknown_code"))

    assert report["activity"] is None
    assert report["build_errors"] == [{"section": "activity", "type": "KeyError"}]
    assert report["session"] is not None


def test_minimal_bundle_validates():
    _validate(sr._minimal_bundle("x_failed", [{"section": "env", "type": "OSError"}]))


def test_a_note_that_fails_the_schema_becomes_the_minimal_note(monkeypatch):
    monkeypatch.setattr(sr, "_env", lambda _session: {"cli_version": "has spaces in it"})

    report = _validate(sr.build_bundle(Session(), code="x_failed", phase="INIT",
                                       next_command=None, reason="unknown_code"))

    assert report["build_errors"] == [{"section": "schema", "type": "SchemaMismatch"}]
    assert report["error"] == {"code": "x_failed"}
    assert "session" not in report


def test_build_failure_means_no_offer_and_the_original_error(runner, checkout, monkeypatch):
    from booking_agent_cli.session import load_session, save_session

    baseline_session = load_session()
    offer_reason = sr.offer_reason
    monkeypatch.setattr(sr, "offer_reason", lambda *_a, **_k: None)
    baseline = _rebook_fails(runner, monkeypatch)
    save_session(baseline_session, write_support_report=True)
    monkeypatch.setattr(sr, "offer_reason", offer_reason)

    def explode(*_a, **_k):
        raise RuntimeError("bundle broke")

    monkeypatch.setattr(sr, "build_bundle", explode)
    broken = _rebook_fails(runner, monkeypatch)

    assert broken.stdout == baseline.stdout
    assert broken.exit_code == baseline.exit_code == 1
    assert _session().support_report_bytes is None


def test_corrupt_session_file_means_no_offer_and_no_traceback(runner, isolated_session):
    from booking_agent_cli.session import session_path

    session_path().write_text("{ definitely not json", encoding="utf-8")

    r = _invoke(runner, "flow", "pay", "rebook", "--service-id", "svc_1")

    assert "support_report_offered" not in r.stdout
    assert "Traceback" not in r.stdout and "Traceback" not in r.stderr


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, headers={"content-type": "application/json"}, content=b"\xff\xfe\xfa"),
        httpx.Response(500, headers={"content-type": "application/json"}, content=b""),
        httpx.Response(500, headers={"content-type": "application/json"}, json=[1, 2, 3]),
        httpx.Response(500, headers={"content-type": "application/problem+json"},
                       stream=httpx.ByteStream(b'{"type": "x"}')),
    ],
    ids=["non_utf8", "empty", "json_array", "streamed"],
)
def test_failure_hook_never_breaks_the_caller(response):
    from booking_agent.http_trace import clear_last_http_failure, last_http_failure, traced_async_client

    clear_last_http_failure()

    async def go():
        async with traced_async_client(transport=httpx.MockTransport(lambda _r: response)) as c:
            r = await c.get("https://api.merchant.test/x")
            return r.status_code, await r.aread()

    status, body = asyncio.run(go())

    assert status == 500
    assert isinstance(body, bytes)
    record = last_http_failure()
    assert record["status"] == 500
    assert record["ucp_message_codes"] == []


def test_uncaught_exception_is_internal_error_without_a_traceback(
    runner, checkout, monkeypatch, capsys,
):
    from booking_agent_cli import main

    def crash(_session):
        raise RuntimeError(f"kaboom for {_BUYER_EMAIL}")

    monkeypatch.setattr(sr, "status_gate", crash)
    monkeypatch.setattr(sys, "argv", ["booking-agent", "flow", "status"])
    capsys.readouterr()

    with pytest.raises(SystemExit) as exit_info:
        main.run()

    captured = capsys.readouterr()
    assert exit_info.value.code == 1
    out = json.loads(captured.out[captured.out.index("{"):])
    assert out["data"]["error"]["code"] == "internal_error"
    assert out["support_report_offered"] is True
    assert out["next_command"] == sr.SUPPORT_REPORT_COMMAND
    for stream in (captured.out, captured.err):
        assert "Traceback" not in stream
        assert _BUYER_EMAIL not in stream
        assert "kaboom" not in stream
    report = _stored()
    assert report["exception"]["type"] == "builtins.RuntimeError"
    assert report["exception"]["frames"][-1]["function"] == "crash"
    assert report["error"]["offer_reason"] == "internal_error"


def test_handled_exception_records_frames(runner, checkout, monkeypatch):
    _offer(runner, monkeypatch, exc=ConnectionError("down"))
    frames = _stored()["exception"]["frames"]

    assert any(f["file"] == "booking_agent_cli/commands/flow.py" and f["function"] == "pay_rebook" for f in frames)


# ---------------------------------------------------------------------------
# The bundle schema the CLI enforces before anything is sent
# ---------------------------------------------------------------------------


def test_default_ingest_url_is_an_apps_script_web_app():
    url = httpx.URL(sr.DEFAULT_SUPPORT_INGEST_URL)
    assert url.scheme == "https" and url.host == "script.google.com"
    assert re.fullmatch(r"/macros/s/[A-Za-z0-9_-]+/exec", url.path)


def _agreement_cases(runner, monkeypatch) -> tuple:
    valid = [
        json.loads(_leaky_offer(runner, monkeypatch)),
        json.loads(sr._minimal_bundle("x_failed", [])),
        json.loads(sr.build_bundle(Session(), code="x_failed", phase="INIT",
                                   next_command=None, reason="unknown_code")),
    ]
    base = valid[1]
    invalid = [
        {**base, "recipient": "attacker@evil.test"},
        {**base, "error": {"code": "x_failed", "message": "free text"}},
        {**base, "error": {}},
        {**base, "bundle_version": 2},
        {**base, "bundle_version": True},
        {**base, "env": {"cli_version": "has spaces in it"}},
        {**base, "created_at": "\u0662\u0660\u0662\u0666-10-11"},
        {k: v for k, v in base.items() if k != "build_errors"},
    ]
    # jsonschema's Python `$` matches before a trailing newline; the CLI's does not.
    cli_only = [{**base, "env": {"cli_version": "x_failed\n"}}]
    return valid, invalid, cli_only


def test_cli_validator_agrees_with_jsonschema(runner, checkout, monkeypatch):
    valid, invalid, cli_only = _agreement_cases(runner, monkeypatch)

    expected = [_VALIDATOR.is_valid(c) for c in valid + invalid]
    assert expected == [True] * len(valid) + [False] * len(invalid)
    assert [not validate_bundle(c) for c in valid + invalid] == expected
    assert all(validate_bundle(c) for c in cli_only)


def test_cli_validator_fails_closed_on_an_unsupported_schema():
    assert validate_bundle({}, {"type": "object", "minProperties": 1}) == [
        "schema: unsupported keyword minProperties",
    ]
    assert validate_bundle({}, {"$ref": "#/$defs/missing"}) == ["schema: unknown $ref #/$defs/missing"]


# ---------------------------------------------------------------------------
# Delivery: the Apps Script redirect is the only one ever followed
# ---------------------------------------------------------------------------

_EXEC = "https://script.google.com/macros/s/AKfyc_test/exec"
_ECHO = "https://script.googleusercontent.com/macros/echo?user_content_key=abc&lib=x"


def _transport(post_response: httpx.Response, echo_response: httpx.Response | None = None):
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.content))
        if request.method == "POST":
            return post_response
        if echo_response is None:
            raise AssertionError(f"unexpected follow to {request.url}")
        return echo_response

    return httpx.MockTransport(handler), seen


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


@pytest.mark.parametrize(
    "echo,expected",
    [
        (httpx.Response(200, json={"accepted": True}), None),
        (httpx.Response(200, json={"accepted": False, "reason": "schema"}), "schema"),
        (httpx.Response(200, json={"accepted": False, "reason": "rate_limited"}), "rate_limited"),
        (httpx.Response(200, json={"accepted": False, "reason": "Alice's note"}), "rejected"),
        (httpx.Response(200, json={"accepted": False}), "rejected"),
        (httpx.Response(200, json=["accepted"]), "rejected"),
        (httpx.Response(200, text="<html>sign in</html>"), "rejected"),
        (httpx.Response(404, text="gone"), "http_404"),
        (httpx.Response(500, text="boom"), "http_500"),
    ],
    ids=[
        "accepted", "schema", "rate_limited", "unknown_reason", "no_reason",
        "not_an_object", "html", "echo_404", "echo_500",
    ],
)
def test_apps_script_reply_decides_delivery(echo, expected):
    transport, seen = _transport(_redirect(_ECHO), echo)

    assert sr._post_report(_EXEC, b'{"a":1}', transport=transport) == expected
    assert seen[0] == ("POST", _EXEC, b'{"a":1}')
    assert seen[1][:2] == ("GET", _ECHO)
    assert len(seen) == 2


@pytest.mark.parametrize(
    "url,location",
    [
        (_EXEC, "https://evil.test/collect"),
        (_EXEC, "http://script.googleusercontent.com/macros/echo"),
        ("https://ingest.example.test/report", _ECHO),
        ("http://script.google.com/macros/s/x/exec", _ECHO),
    ],
    ids=["other_target_host", "plain_http_target", "other_origin_host", "plain_http_origin"],
)
def test_no_other_redirect_is_followed(url, location):
    transport, seen = _transport(_redirect(location))

    assert sr._post_report(url, b"{}", transport=transport) == "http_302"
    assert [m for m, _u, _b in seen] == ["POST"]


def test_direct_responses_are_unchanged():
    for status, expected in ((200, None), (202, None), (500, "http_500")):
        transport, seen = _transport(httpx.Response(status))
        assert sr._post_report("https://ingest.example.test/report", b"{}", transport=transport) == expected
        assert len(seen) == 1


def test_send_through_the_apps_script_redirect(runner, checkout, monkeypatch):
    import functools

    transport, seen = _transport(_redirect(_ECHO), httpx.Response(200, json={"accepted": True}))
    monkeypatch.setattr(sr, "_post_report", functools.partial(sr._post_report, transport=transport))
    _offer(runner, monkeypatch)
    stored = _session().support_report_bytes

    out = _stdout_json(_invoke(runner, "flow", "support-report", "send", "--user-confirmed"))

    assert out["data"]["sent"] is True
    assert seen[0] == ("POST", sr.DEFAULT_SUPPORT_INGEST_URL, stored.encode("utf-8"))
    assert _session().support_report_bytes is None


def test_rejected_by_the_ingest_keeps_the_note(runner, checkout, monkeypatch):
    import functools

    transport, _seen = _transport(
        _redirect(_ECHO), httpx.Response(200, json={"accepted": False, "reason": "schema"}),
    )
    monkeypatch.setattr(sr, "_post_report", functools.partial(sr._post_report, transport=transport))
    _offer(runner, monkeypatch)

    out = _stdout_json(_invoke(runner, "flow", "support-report", "send", "--user-confirmed"))

    assert out["data"]["error"]["code"] == "support_report_send_failed"
    assert "(schema)" in out["data"]["error"]["message"]
    assert "422" not in out["data"]["error"]["message"]
    assert out["user_message"] == sr.SEND_FAILED_MESSAGE
    assert _session().support_report_bytes


# ---------------------------------------------------------------------------
# Skill contract
# ---------------------------------------------------------------------------

def _skill_text() -> str:
    return _SKILL.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end, text.index(start))]


def _table_codes(section: str) -> set:
    codes = set()
    for line in section.splitlines():
        if line.startswith("| `"):
            first_cell = line.split("|")[1]
            codes.update(re.findall(r"`([a-z_]+)`", first_cell))
        elif line.startswith("| anything else"):
            codes.update(re.findall(r"`([a-z_]+)`", line.split("|")[1]))
    return codes - {"recoverable"}


def test_expected_codes_match_the_skill_tables():
    text = _skill_text()
    errors = _section(text, "## Errors", "### Notes to the assistant team")
    matrix = _section(text, "**Step 6c-iii", "**Payment taken, booking not confirmed")
    cancel = _section(text, "## Cancel upcoming bookings", "## Reschedule an upcoming booking")

    tables = _table_codes(errors) | _table_codes(matrix) | _table_codes(cancel)
    assert tables == set(sr.SKILL_TABLE_CODES)
    for code in sr.SKILL_PROSE_CODES:
        assert f"`{code}`" in text, code


def test_skill_version_constant_matches_the_frontmatter():
    match = re.search(r"^version:\s*(\S+)", _skill_text(), re.MULTILINE)
    assert match and match.group(1) == sr.SKILL_VERSION


_BANNED = re.compile(r"\b(github|issue|issues|log|logs|cli|session|sessions)\b", re.IGNORECASE)


def test_the_ask_and_the_skill_paragraph_use_plain_words():
    paragraph = _section(_skill_text(), "### Notes to the assistant team", "## Booking authorization")
    prose = re.sub(r"`[^`]*`", "", paragraph)

    for text in (sr.CONSENT_MESSAGE, sr.RETRY_CONSENT_MESSAGE, sr.SENT_MESSAGE,
                 sr.SEND_FAILED_MESSAGE, prose):
        assert not _BANNED.search(text), _BANNED.search(text)


def test_skill_agent_loop_handles_the_consent_gate():
    text = _skill_text()
    loop = _section(text, "## Agent loop", "## Core flow")

    assert "`support_report_consent`" in loop
    assert "support_report_offered: true" in loop
    assert "booking-agent flow support-report decline" in loop