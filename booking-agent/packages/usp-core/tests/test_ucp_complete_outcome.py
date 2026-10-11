"""Classification of complete_checkout bodies (spec #93 Track A branch rule)."""
from __future__ import annotations

import pytest

from booking_agent.ucp_checkout import (
    CompleteOutcome,
    UcpCompleteOutcomeError,
    UcpProfileValidationError,
    classify_complete_checkout,
    parse_checkout_error_body,
)

_HOSTED = "https://merchant.test/checkout/chk_1"


def _msg(code, severity=None, content="The payment could not be processed."):
    m = {"type": "error", "code": code, "content": content}
    if severity is not None:
        m["severity"] = severity
    return m


def _body(*messages, status="incomplete", **extra):
    return {
        "id": "chk_1",
        "status": status,
        "continue_url": _HOSTED,
        "messages": list(messages),
        **extra,
    }


@pytest.mark.parametrize(
    "message",
    [
        _msg("payment_declined"),
        _msg("payment_declined", "recoverable"),
        _msg("payment_declined", "requires_buyer_review"),
        _msg("payment_failed", "recoverable"),
        _msg("payment_authentication_unsupported", "recoverable"),
        _msg("payment_not_processed", "recoverable"),
    ],
)
def test_decline_codes_classify_as_declined(message):
    result = classify_complete_checkout(_body(message))
    assert result.outcome is CompleteOutcome.DECLINED
    assert result.message == message


def test_escalated_legacy_decline_is_still_a_decline():
    body = _body(_msg("payment_declined"), status="requires_escalation")
    assert classify_complete_checkout(body).outcome is CompleteOutcome.DECLINED


@pytest.mark.parametrize("severity", [None, "requires_buyer_input"])
def test_payment_failed_needs_recoverable_severity(severity):
    body = _body(_msg("payment_failed", severity))
    assert classify_complete_checkout(body).outcome is CompleteOutcome.UNCLASSIFIED


def test_unrecoverable_payment_failed_is_payment_unrecoverable():
    message = _msg("payment_failed", "unrecoverable")
    result = classify_complete_checkout(_body(message, status="ready_for_complete"))
    assert result.outcome is CompleteOutcome.PAYMENT_UNRECOVERABLE
    assert result.outcome.value == "payment_unrecoverable"
    assert result.message == message


def test_payment_unrecoverable_wins_over_decline():
    body = _body(_msg("payment_declined"), _msg("payment_failed", "unrecoverable"))
    assert classify_complete_checkout(body).outcome is CompleteOutcome.PAYMENT_UNRECOVERABLE


def test_captured_unconfirmed_wins_over_payment_unrecoverable():
    body = _body(
        _msg("payment_failed", "unrecoverable"),
        _msg("payment_captured_booking_unconfirmed", "unrecoverable"),
    )
    assert classify_complete_checkout(body).outcome is CompleteOutcome.CAPTURED_UNCONFIRMED


def test_unrecoverable_severity_alone_is_not_payment_unrecoverable():
    body = _body(_msg("payment_captured_completion_failed", "unrecoverable"))
    assert classify_complete_checkout(body).outcome is CompleteOutcome.UNCLASSIFIED


def test_continue_url_alone_is_not_a_decline():
    body = _body()
    assert classify_complete_checkout(body).outcome is CompleteOutcome.UNCLASSIFIED


def test_incomplete_status_alone_is_not_a_decline():
    body = {"id": "chk_1", "status": "incomplete"}
    assert classify_complete_checkout(body).outcome is CompleteOutcome.UNCLASSIFIED


@pytest.mark.parametrize("code", ["complete_in_progress", "checkout_already_in_progress"])
def test_in_progress_wins_over_decline(code):
    body = _body(_msg("payment_declined"), _msg(code))
    assert classify_complete_checkout(body).outcome is CompleteOutcome.IN_PROGRESS


def test_captured_unconfirmed_wins_over_everything_but_success():
    body = _body(
        _msg("payment_declined"),
        _msg("complete_in_progress"),
        _msg("payment_captured_booking_unconfirmed", "unrecoverable"),
    )
    assert classify_complete_checkout(body).outcome is CompleteOutcome.CAPTURED_UNCONFIRMED


def test_completed_but_unconfirmed_booking_is_captured_unconfirmed():
    body = {"status": "completed", "booking": {"booking_status": "pending"}}
    assert classify_complete_checkout(body).outcome is CompleteOutcome.CAPTURED_UNCONFIRMED


def test_mandatory_acceptance_keeps_continue_url():
    body = _body(_msg("mandatory_acceptance_pending", "requires_buyer_review"))
    result = classify_complete_checkout(body)
    assert result.outcome is CompleteOutcome.MANDATORY_ACCEPTANCE
    assert result.continue_url == _HOSTED


def test_mandatory_acceptance_wins_over_decline():
    body = _body(_msg("payment_declined"), _msg("mandatory_acceptance_pending"))
    assert classify_complete_checkout(body).outcome is CompleteOutcome.MANDATORY_ACCEPTANCE


def test_completed_confirmed_is_success():
    body = {
        "status": "completed",
        "booking": {"booking_status": "confirmed"},
        "messages": [_msg("payment_declined")],
    }
    assert classify_complete_checkout(body).outcome is CompleteOutcome.COMPLETED


@pytest.mark.parametrize("body", [None, "nope", [], {"messages": "x"}])
def test_garbage_is_unclassified(body):
    assert classify_complete_checkout(body).outcome is CompleteOutcome.UNCLASSIFIED


def test_parse_checkout_error_body():
    assert parse_checkout_error_body('{"status": "incomplete"}') == {"status": "incomplete"}
    assert parse_checkout_error_body("card declined") is None
    assert parse_checkout_error_body("[1]") is None
    assert parse_checkout_error_body("") is None


def test_outcome_error_is_a_validation_error_without_urls_in_text():
    result = classify_complete_checkout(_body(_msg("payment_declined")))
    err = UcpCompleteOutcomeError.from_classification(result, checkout_id="chk_1")
    assert isinstance(err, UcpProfileValidationError)
    assert err.outcome is CompleteOutcome.DECLINED
    assert err.code == "payment_declined"
    assert err.checkout_id == "chk_1"
    assert err.continue_url == _HOSTED
    assert _HOSTED not in str(err)
