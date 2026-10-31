"""The relay eval set: what the CLI hands the agent, what it accepts back, and what SKILL.md says."""
import json
from pathlib import Path

import pytest

from integration.test_booking_questions_flow import (
    _SERVICE_ID,
    _err,
    _ok,
    _select_first,
    _start,
    questions_on,  # noqa: F401
)
from integration.test_flow_gates import _invoke

_TESTS = Path(__file__).resolve().parents[1]
_EVALS = json.loads((_TESTS / "evals" / "booking_questions_relay.json").read_text())
_SKILL = (_TESTS.parents[2] / "skills" / "booking-agent" / "SKILL.md").read_text()


def _set_for(item):
    return [{
        "id": "relay", "revision": "r1", "purpose": "intake",
        "completion_timing": "before_confirmation", "frequency": "every_booking",
        "items": [item],
    }]


def _answer(runner, item, args):
    return _invoke(runner, "flow", "questions", "answer", "--set-id", "relay",
                   "--question-id", item["id"], *args)


@pytest.mark.parametrize("case", _EVALS["cases"], ids=[c["id"] for c in _EVALS["cases"]])
def test_relay_case(runner, isolated_session, monkeypatch, questions_on, case):  # noqa: F811
    from booking_agent_cli import booking_questions_flow as bq_flow
    from booking_agent_cli.session import must_load

    item = case["item"]
    _start(runner, monkeypatch, _set_for(item))
    _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    _select_first(runner)
    asked = _ok(_invoke(runner, "flow", "questions", "start"))
    question = asked["data"]["booking_question"]
    expect = case["expect"]
    assert question["untrusted_business_text"] is True
    if "label_plain" in expect:
        assert question["label"]["plain"] == expect["label_plain"]
        assert "word for word" in asked["prompt"]
    if "host_form_allowed" in expect:
        assert question["host_form_allowed"] is expect["host_form_allowed"]
    if "sensitive" in expect:
        assert question["sensitive"] is expect["sensitive"]
    if "option_count" in expect:
        assert len(question["options"]) == expect["option_count"]

    for reply in case.get("replies", []):
        result = _answer(runner, item, reply["args"])
        if reply["outcome"] in ("refused_label_not_id", "reasked"):
            _err(result, "booking_question_answer_invalid")
            assert bq_flow.answers(must_load(), "relay") == {}
            continue
        _ok(result)
        stored = bq_flow.answers(must_load(), "relay")[item["id"]]
        expected = reply["args"][1]
        assert stored == expected
        if reply["outcome"] == "stored_verbatim":
            assert stored == reply["reply"]


@pytest.mark.parametrize("rule", _EVALS["skill_rules"])
def test_skill_states_the_relay_rule(rule):
    assert rule in _SKILL
