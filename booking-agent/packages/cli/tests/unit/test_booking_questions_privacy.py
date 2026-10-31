"""Answers to a business's questions stay out of logs, traces and the saved session."""
import json

from booking_agent.http_trace import _redact_body_text
from booking_agent_cli import booking_questions_flow as bq_flow
from booking_agent_cli.activity_log import format_argv
from booking_agent_cli.engine import transition
from booking_agent_cli.session import Session

_SETS = [{
    "id": "intake", "revision": "r1", "purpose": "intake",
    "completion_timing": "before_confirmation", "frequency": "every_booking",
    "items": [
        {"kind": "question", "id": "injury", "label": {"plain": "Any injuries?"},
         "schema": {"type": "string", "maxLength": 200}, "required": True, "sensitive": True},
        {"kind": "question", "id": "pressure", "label": {"plain": "Pressure"},
         "schema": {"type": "string", "oneOf": [{"const": "soft", "title": "Soft"},
                                                {"const": "firm", "title": "Firm"}]},
         "required": True},
    ],
}]


def _session() -> Session:
    s = Session()
    s.booking_questions = {
        "surfaces": ["bookings", "catalog"], "service_id": "svc", "sets": json.loads(json.dumps(_SETS)),
    }
    assert bq_flow.record_answer(s, "intake", "injury", "Sprained left ankle") == []
    assert bq_flow.record_answer(s, "intake", "pressure", "firm") == []
    return s


def test_activity_log_redacts_typed_answers_choices_and_yes_no():
    text = format_argv([
        "booking-agent", "flow", "questions", "answer", "--set-id", "intake",
        "--question-id", "injury", "--value", "Sprained left ankle",
    ])
    assert "Sprained" not in text and "<redacted>" in text
    assert "--question-id injury" in text
    choice = format_argv(["booking-agent", "flow", "questions", "answer", "--option-id", "firm"])
    assert "firm" not in choice
    yes = format_argv(["booking-agent", "flow", "questions", "answer", "--question-id", "q", "--yes"])
    assert "--yes" not in yes
    assert "--yes" in format_argv(["booking-agent", "flow", "calendar", "skip", "--yes"])


def test_http_trace_redacts_answers_and_recipient():
    body = json.dumps({
        "recipient": {"first_name": "Noa", "last_name": "Levi", "email": "noa@example.com"},
        "booking_question_submissions": [
            {"set_id": "intake", "revision": "r1", "answers": {"injury": "Sprained left ankle"}},
        ],
        "booking_question_assessment_token": "bqat_secret",
    })
    redacted = _redact_body_text(body)
    for secret in ("Sprained", "Noa", "noa@example.com", "bqat_secret"):
        assert secret not in redacted
    assert '"set_id": "intake"' in redacted


def test_recap_shows_a_sensitive_answer_only_as_provided():
    lines = bq_flow.recap_lines(_session())
    assert "Any injuries?: provided" in lines
    assert "Pressure: Firm" in lines
    assert not any("Sprained" in line for line in lines)


def test_confirmation_erases_sensitive_answers_and_keeps_the_marker():
    s = _session()
    bq_flow.set_buyer_override(s, "phone_number", "+14155550123")
    transition(s, to="CONFIRMED")
    saved = json.dumps(s.booking_questions)
    assert "Sprained" not in saved and "+14155550123" not in saved
    assert bq_flow.provided(s, "intake") == ["injury"]
    assert "Any injuries?: provided" in bq_flow.recap_lines(s)
    assert bq_flow.answers(s, "intake") == {"pressure": "firm"}


def test_calendar_choice_survives_erasure_on_later_event_updates():
    s = _session()
    s.booking_questions["calendar_choice"] = "all"
    before = bq_flow.calendar_lines(s)
    assert "Any injuries?: Sprained left ankle" in before
    transition(s, to="CONFIRMED")
    assert bq_flow.calendar_lines(s) == before
    s2 = _session()
    s2.booking_questions["calendar_choice"] = "non_sensitive"
    assert bq_flow.calendar_lines(s2) == ["Pressure: Firm"]


def test_recipient_stays_out_of_the_activity_log_and_support_report_and_loses_contact_at_confirmation():
    from booking_agent_cli.support_report import project_session

    text = format_argv([
        "booking-agent", "flow", "recipient", "set", "--first-name", "Noa",
        "--last-name", "Levi", "--email", "noa@example.com",
    ])
    assert "Noa" not in text and "Levi" not in text and "noa@example.com" not in text
    s = Session()
    s.recipient = {"first_name": "Noa", "last_name": "Levi", "email": "noa@example.com",
                   "phone_number": "+14155550123"}
    s.recipient_pending = {"first_name": "Tom", "last_name": "Levi"}
    projected = json.dumps(project_session(s))
    assert "Noa" not in projected and "Tom" not in projected
    transition(s, to="CONFIRMED")
    assert s.recipient == {"first_name": "Noa", "last_name": "Levi"}


def test_a_changed_recipient_changes_what_the_prepared_checkout_was_made_for():
    from booking_agent_cli.payment_helpers import checkout_inputs_extra

    s = Session()
    assert checkout_inputs_extra(s) == {}
    s.recipient = {"first_name": "Noa", "last_name": "Levi"}
    noa = checkout_inputs_extra(s)
    s.recipient = {"first_name": "Tom", "last_name": "Levi"}
    assert checkout_inputs_extra(s) != noa
    assert checkout_inputs_extra(s)["recipient"] == {"first_name": "Tom", "last_name": "Levi"}


def test_dormant_session_is_untouched_by_confirmation():
    s = Session()
    transition(s, to="CONFIRMED")
    assert s.booking_questions == {}
