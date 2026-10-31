"""Booking questions through the CLI flow, against the in-memory business.

The extension is switched on by patching activation, since the hosted platform profile
does not declare it yet. Each test drives the same commands an agent runs.
"""
import json
from pathlib import Path

import pytest

from booking_agent.models.wire import USPService
from support.fixtures import SERVICE_BY_ID
from support.fixtures.services import set_zero_price_free_service
from support.mock import backend as mock_backend
from support.mock.link_cli import YUMA_SHIPPING_ADDRESS, mock_link_cli_user_info
from integration.test_flow_gates import (
    _BOOK,
    _BUYER,
    _SERVICE_ID,
    _YUMA,
    _business_use,
    _geocoder,
    _init,
    _invoke,
    _satisfy_link_auth_gate,
    _skip_calendar,
    _stdout_json,
)

_VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "booking_questions" / "flow"
_ACTIVE = frozenset({"catalog", "availability", "bookings"})


def _flow_sets(name):
    return json.loads((_VECTORS / f"{name}.json").read_text())["given"]["service"]["booking_question_sets"]


def _ok(result):
    assert result.exit_code == 0, result.stdout + result.stderr
    return _stdout_json(result)


def _err(result, code):
    assert result.exit_code == 1, result.stdout + result.stderr
    body = _stdout_json(result)
    assert body["data"]["error"]["code"] == code, body
    return body


@pytest.fixture
def questions_on(monkeypatch):
    import booking_agent.booking_questions_negotiation as negotiation

    async def active(_business_usp):
        return _ACTIVE

    monkeypatch.setattr(negotiation, "booking_questions_activation", active)


def _with_sets(monkeypatch, sets, **extra):
    service = SERVICE_BY_ID[_SERVICE_ID]
    patched = USPService.model_validate(
        {**service.model_dump(mode="json", by_alias=True, exclude_none=True),
         "booking_question_sets": sets, **extra}
    )
    monkeypatch.setitem(SERVICE_BY_ID, _SERVICE_ID, patched)


def _start(runner, monkeypatch, sets, **extra):
    set_zero_price_free_service(monkeypatch, _SERVICE_ID)
    _with_sets(monkeypatch, sets, **extra)
    mock_link_cli_user_info(monkeypatch, _BUYER, shipping_addresses=[YUMA_SHIPPING_ADDRESS])
    _geocoder(monkeypatch, _YUMA)
    _init(runner)
    _ok(_business_use(runner))
    _ok(_skip_calendar(runner))


def _select_first(runner):
    slots = _ok(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _ok(_invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed"))
    _satisfy_link_auth_gate()


def _calls(method):
    return [kw for m, kw in mock_backend.BQ_CALLS if m == method]


def test_before_confirmation_questions_are_asked_then_sent_on_create(
    runner, isolated_session, monkeypatch, questions_on,
):
    from booking_agent_cli.session import must_load

    _start(runner, monkeypatch, _flow_sets("bq-flow-hair-salon-allergy-branch"))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)

    pending = _err(_invoke(runner, "flow", "buyer", "resolve"), "booking_questions_pending")
    assert pending["next_command"] == "booking-agent flow questions start"

    first = _ok(_invoke(runner, "flow", "questions", "start"))
    question = first["data"]["booking_question"]
    assert first["awaiting_user"] == "booking_question"
    assert question["question_id"] == "hair_length"
    assert question["untrusted_business_text"] is True
    assert [o["id"] for o in question["options"]] == ["short", "medium", "long"]

    bad = _invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                  "--question-id", "hair_length", "--option-id", "purple")
    assert "option" in " ".join(_err(bad, "booking_question_answer_invalid")["data"]["failed_rules"])

    second = _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                         "--question-id", "hair_length", "--option-id", "long"))
    assert second["data"]["booking_question"]["question_id"] == "color_allergy"
    third = _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                        "--question-id", "color_allergy", "--yes"))
    assert third["data"]["booking_question"]["question_id"] == "allergy_detail"
    done = _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                       "--question-id", "allergy_detail", "--value", "Itching after a dark dye"))
    assert done["next_command"] == "booking-agent flow buyer resolve"

    recap = _ok(_invoke(runner, "flow", "buyer", "resolve"))
    lines = recap["data"]["booking_recap_lines"]
    assert "Hair length: Long" in lines
    assert "Have you reacted to hair color before?: Yes" in lines
    assert "What happened?: Itching after a dark dye" in lines

    _ok(_invoke(runner, *_BOOK))
    [create] = _calls("create_booking")
    assert create["booking_question_submissions"] == [{
        "set_id": "intake", "revision": "r1",
        "answers": {"hair_length": "long", "color_allergy": True,
                    "allergy_detail": "Itching after a dark dye"},
        "collected_via": {"hair_length": "conversation", "color_allergy": "conversation",
                          "allergy_detail": "conversation"},
    }]
    assert must_load().booking.id


def test_changed_answer_after_summary_needs_a_new_yes(
    runner, isolated_session, monkeypatch, questions_on,
):
    _start(runner, monkeypatch, _flow_sets("bq-flow-hair-salon-allergy-branch"))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)
    for qid, flag in (("hair_length", ("--option-id", "short")), ("color_allergy", ("--no",))):
        _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                    "--question-id", qid, *flag))
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                "--question-id", "hair_length", "--option-id", "medium"))
    _err(_invoke(runner, *_BOOK), "booking_confirmation_required")
    assert _calls("create_booking") == []


def test_preflight_gates_availability_and_threads_the_token(
    runner, isolated_session, monkeypatch, questions_on,
):
    _start(runner, monkeypatch, _flow_sets("bq-flow-eligible-token"))
    gate = _err(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID),
                "booking_eligibility_required")
    assert gate["next_command"] == "booking-agent flow questions start --phase preflight"

    q = _ok(_invoke(runner, "flow", "questions", "start", "--phase", "preflight"))
    assert q["data"]["booking_question"]["question_id"] == "prior_reaction"
    eligible = _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "eligibility",
                           "--question-id", "prior_reaction", "--no"))
    assert eligible["next_command"] == f"booking-agent flow availability --service-id {_SERVICE_ID}"
    [check] = _calls("check_eligibility")
    assert check["submissions"][0]["answers"] == {"prior_reaction": False}

    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    assert _calls("query_availability")[0] == {"booking_question_assessment_token": "bqat_mock"}
    _select_first(runner)
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    _ok(_invoke(runner, *_BOOK))
    [create] = _calls("create_booking")
    assert create["booking_question_assessment_token"] == "bqat_mock"


def test_ineligible_decision_stops_before_times(
    runner, isolated_session, monkeypatch, questions_on,
):
    _start(runner, monkeypatch, _flow_sets("bq-flow-eligible-token"))
    mock_backend.ELIGIBILITY.append({
        "decision": "ineligible",
        "messages": [{"type": "error", "code": "booking_ineligible",
                      "content": "This service is for adults only.", "severity": "unrecoverable"}],
    })
    _ok(_invoke(runner, "flow", "questions", "start", "--phase", "preflight"))
    body = _err(_invoke(runner, "flow", "questions", "answer", "--set-id", "eligibility",
                        "--question-id", "prior_reaction", "--yes"), "booking_ineligible")
    assert body["data"]["business_messages"] == ["This service is for adults only."]
    again = _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    _err(again, "booking_ineligible")
    assert _calls("query_availability") == []


def test_required_buyer_field_is_asked_and_sent(
    runner, isolated_session, monkeypatch, questions_on,
):
    _start(runner, monkeypatch, [], required_buyer_fields=["phone_number"])
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)
    pend = _err(_invoke(runner, "flow", "buyer", "resolve"), "booking_questions_pending")
    assert pend["data"]["pending"] == {"buyer_fields": ["phone_number"]}, pend["data"]
    ask = _ok(_invoke(runner, "flow", "questions", "start"))
    assert ask["awaiting_user"] == "buyer_field", ask
    assert ask["data"]["buyer_field"] == "phone_number"
    _ok(_invoke(runner, "flow", "questions", "answer", "--buyer-field", "phone_number",
                "--value", "+14155550123"))
    recap = _ok(_invoke(runner, "flow", "buyer", "resolve"))
    assert "Phone: +14155550123" in recap["data"]["booking_recap_lines"]
    _ok(_invoke(runner, *_BOOK))
    from booking_agent_cli.session import must_load

    booking = mock_backend.BOOKINGS[must_load().booking.id]
    assert booking.buyer.phone_number == "+14155550123"


def test_notes_flag_is_refused_when_a_question_maps_to_notes(
    runner, isolated_session, monkeypatch, questions_on,
):
    _start(runner, monkeypatch, _flow_sets("bq-flow-maps-to-notes-no-duplicate"))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)
    body = _err(_invoke(runner, "flow", "buyer", "resolve", "--notes", "Bring the dog"),
                "notes_asked_as_question")
    assert "flow questions answer" in body["next_command"]


def _ack_action_on_create(monkeypatch, *, continue_url=None):
    """The business answers create with a pending buyer-input Action for ``no_show_fee``."""
    from booking_agent.models.wire import Action, BookingStatus
    from booking_agent.namespace import ACTION_BOOKING_QUESTIONS_BUYER_INPUT

    original = mock_backend.MockUSPBackend.create_booking

    async def with_action(self, *args, **kwargs):
        booking = await original(self, *args, **kwargs)
        action = Action.model_validate({
            "type": ACTION_BOOKING_QUESTIONS_BUYER_INPUT,
            "id": "act_bq_1",
            "status": "pending",
            "config": {"mode": "acknowledge", "sets": [{"set_id": "intake", "revision": "r1"}],
                       "item_ids": ["no_show_fee"], "presentation": "verbatim"},
            **({"continue_url": continue_url} if continue_url else {}),
        })
        booking = booking.model_copy(update={
            "status": BookingStatus.REQUIRES_ACTION, "actions": [action],
        })
        mock_backend.BOOKINGS[booking.id] = booking
        return booking

    monkeypatch.setattr(mock_backend.MockUSPBackend, "create_booking", with_action)


def _book_with_action(runner, monkeypatch):
    _start(runner, monkeypatch, _flow_sets("bq-flow-action-platform-processing"))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)
    _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                "--question-id", "hair_notes", "--value", "Fine hair"))
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    return _ok(_invoke(runner, *_BOOK))


def test_buyer_input_action_needs_the_users_explicit_yes(
    runner, isolated_session, monkeypatch, questions_on,
):
    _ack_action_on_create(monkeypatch)
    asked = _book_with_action(runner, monkeypatch)
    assert asked["awaiting_user"] == "booking_question_action"
    assert asked["data"]["verbatim_lines"] == ["I agree to the no-show fee of $30."]
    assert "I agree to the no-show fee of $30." in asked["user_message"]
    accept = asked["next_command"].split()

    stale = list(accept)
    stale[stale.index("--content-digest") + 1] = "0" * 64
    _err(_invoke(runner, *stale[1:]), "booking_question_action_not_confirmed")
    assert _calls("update_booking") == []

    done = _ok(_invoke(runner, *accept[1:]))
    assert done["next_command"] == "booking-agent flow book poll --wait"
    [update] = _calls("update_booking")
    assert update["booking_question_submissions"] == [{
        "set_id": "intake", "revision": "r1", "answers": {"no_show_fee": True},
        "action_id": "act_bq_1", "collected_via": {"no_show_fee": "platform_surface"},
    }]


def test_buyer_input_action_can_go_to_the_business_page(
    runner, isolated_session, monkeypatch, questions_on,
):
    _ack_action_on_create(monkeypatch, continue_url="https://studio.example.com/forms/sess_42")
    _book_with_action(runner, monkeypatch)
    handed = _ok(_invoke(runner, "flow", "questions", "action", "--action-id", "act_bq_1",
                         "--handoff"))
    assert "https://studio.example.com/forms/sess_42" in handed["user_message"]
    assert handed["next_command"] == "booking-agent flow book poll --wait"
    assert _calls("update_booking") == []


def test_rejected_answers_are_asked_again_with_the_business_reason(
    runner, isolated_session, monkeypatch, questions_on,
):
    from booking_agent.rest_client import BookingQuestionsRejected

    calls = {"n": 0}
    original = mock_backend.MockUSPBackend.create_booking

    async def reject_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise BookingQuestionsRejected([{
                "type": "error", "code": "booking_question_answer_missing",
                "content": "This answer is required.", "severity": "recoverable",
                "path": "$.booking_question_submissions[?@.set_id=='intake'].answers['hair_length']",
            }])
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(mock_backend.MockUSPBackend, "create_booking", reject_once)
    _start(runner, monkeypatch, _flow_sets("bq-flow-hair-salon-allergy-branch"))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)
    for qid, flag in (("hair_length", ("--option-id", "short")), ("color_allergy", ("--no",))):
        _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                    "--question-id", qid, *flag))
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    body = _err(_invoke(runner, *_BOOK), "booking_questions_rejected")
    assert body["data"]["reask"] == [
        {"set_id": "intake", "question_id": "hair_length", "code": "booking_question_answer_missing"},
    ]
    again = _ok(_invoke(runner, "flow", "questions", "start"))
    question = again["data"]["booking_question"]
    assert question["question_id"] == "hair_length"
    assert question["business_message"] == "This answer is required."
    done = _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                       "--question-id", "hair_length", "--option-id", "long"))
    assert done["next_command"] == "booking-agent flow buyer resolve"
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    _ok(_invoke(runner, *_BOOK))
    assert calls["n"] == 2


def test_answers_go_to_the_calendar_only_when_the_user_says_so(
    runner, isolated_session, monkeypatch, questions_on,
):
    from booking_agent.calendar.scope import CALENDAR_MODE_EVENTS
    from booking_agent_cli.calendar_event import build_event_body
    from booking_agent_cli.session import load_store, must_load, save_store

    _start(runner, monkeypatch, _flow_sets("bq-flow-hair-salon-allergy-branch"))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)
    store = load_store()
    store.sessions[0].calendar_mode = CALENDAR_MODE_EVENTS
    store.sessions[0].calendar_state = "connected"
    save_store(store)

    _err(_invoke(runner, "flow", "questions", "answers-to-calendar", "--choice", "all"),
         "answers_to_calendar_not_offered")
    _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                "--question-id", "hair_length", "--option-id", "long"))
    offer = _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "intake",
                        "--question-id", "color_allergy", "--no"))
    assert offer["awaiting_user"] == "answers_to_calendar"
    assert offer["next_command"].endswith("--choice all|none")

    done = _ok(_invoke(runner, "flow", "questions", "answers-to-calendar", "--choice", "all"))
    assert done["next_command"] == "booking-agent flow buyer resolve"
    recap = _ok(_invoke(runner, "flow", "buyer", "resolve"))
    assert "Answers copied to your calendar event: all" in recap["data"]["booking_recap_lines"]
    body, _ = build_event_body(must_load())
    assert "Your answers:\nHair length: Long\nHave you reacted to hair color before?: No" in body["description"]


def test_recipient_preflight_is_about_that_person_and_goes_on_the_check(
    runner, isolated_session, monkeypatch, questions_on,
):
    _start(runner, monkeypatch, _flow_sets("bq-flow-kids-class-recipient-age-gate"))
    _ok(_invoke(runner, "flow", "recipient", "set", "--first-name", "Noa", "--last-name", "Levi"))
    q = _ok(_invoke(runner, "flow", "questions", "start", "--phase", "preflight"))
    payload = q["data"]["booking_question"]
    assert payload["subject"] == "recipient"
    assert payload["subject_first_name"] == "Noa"
    _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "participant",
                "--question-id", "participant_name", "--value", "Noa Levi"))
    _ok(_invoke(runner, "flow", "questions", "answer", "--set-id", "participant",
                "--question-id", "birth_date", "--value", "2018-04-12"))
    [check] = _calls("check_eligibility")
    assert check["recipient"] == {"first_name": "Noa", "last_name": "Levi"}
    assert check["submissions"][0]["answers"] == {
        "participant_name": "Noa Levi", "birth_date": "2018-04-12",
    }


def test_dormant_business_sends_nothing_new(runner, isolated_session, monkeypatch):
    from booking_agent_cli.session import must_load

    _start(runner, monkeypatch, _flow_sets("bq-flow-hair-salon-allergy-branch"))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)
    _ok(_invoke(runner, "flow", "buyer", "resolve"))
    _ok(_invoke(runner, *_BOOK))
    assert mock_backend.BQ_CALLS == []
    assert must_load().booking_questions == {}
