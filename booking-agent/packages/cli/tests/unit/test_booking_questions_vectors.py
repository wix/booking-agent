"""The vendored booking-questions ``eval`` and ``wire`` vectors, run against this client."""
from __future__ import annotations

import json
import pathlib
from datetime import date

import pytest
from pydantic import TypeAdapter

from booking_agent import booking_questions as bq
from booking_agent.models import booking_questions as models

VECTORS = pathlib.Path(__file__).resolve().parents[1] / "vectors" / "booking_questions"

MODEL_FOR = {
    "BookingQuestion": models.BookingQuestion,
    "BookingQuestionSet": models.BookingQuestionSet,
    "BookingQuestionExternalTask": models.BookingQuestionExternalTask,
    "BookingQuestionNotice": models.BookingQuestionNotice,
}


def _load(kind: str):
    return [json.loads(p.read_text()) for p in sorted((VECTORS / kind).glob("bq-*.json"))]


def _eval_cases():
    for vector in _load("eval"):
        for case in vector["cases"]:
            yield pytest.param(vector, case, id=f"{vector['id']}:{case['case']}")


@pytest.mark.parametrize("vector,case", list(_eval_cases()))
def test_eval_vector(vector, case):
    qset = case.get("set", vector["given"]["set"])
    then = case["then"]
    errors = bq.definition_errors(qset)
    assert (not errors) == then["definition_valid"], errors
    if "semantic_errors" in then:
        assert sorted(bq.semantic_set_errors(qset)) == sorted(then["semantic_errors"])
    if "answers" not in case:
        return
    result = bq.evaluate_set(qset, case["answers"])
    for key in ("applicable", "required_unanswered", "pruned"):
        if key in then:
            assert result[key] == then[key], key


def _wire_vectors():
    for vector in _load("wire"):
        yield pytest.param(vector, id=vector["id"])


@pytest.mark.parametrize("vector", list(_wire_vectors()))
def test_wire_vector_parses_and_validates_answers(vector):
    name = vector["schema"].rpartition("/")[2]
    instance = vector["instance"]
    if vector["expect"] == "accept" and vector["then"]["conforming"]:
        if name in MODEL_FOR:
            MODEL_FOR[name].model_validate(instance)
        elif name == "BookingQuestionSets":
            TypeAdapter(list[models.BookingQuestionSet]).validate_python(instance)
        assert not bq.semantic_errors_for(name, instance)
    for answer in vector.get("answer_cases", []):
        if answer.get("absent"):
            continue
        question = answer.get("question") or instance
        if isinstance(question, str):
            question = _find_question(instance, question)
        request_date = (
            date.fromisoformat(answer["request_date"]) if answer.get("request_date") else None
        )
        errors = bq.answer_errors(question, answer["value"], request_date)
        if answer["valid"]:
            assert errors == [], (answer, errors)
        else:
            assert errors, answer
            if "errors" in answer:
                assert sorted(errors) == sorted(answer["errors"]), (answer, errors)


@pytest.mark.parametrize("vector", list(_wire_vectors()))
def test_wire_vector_semantic_variants_are_rejected(vector):
    for variant in vector.get("variants", []):
        if variant.get("expect") != "reject" or "semantic_errors" not in variant.get("then", {}):
            continue
        name = vector["schema"].rpartition("/")[2]
        assert sorted(bq.semantic_errors_for(name, variant["instance"])) == sorted(
            variant["then"]["semantic_errors"]
        ), variant["case"]


def _find_question(container, question_id):
    items = container.get("items") if isinstance(container, dict) else None
    sets = container if isinstance(container, list) else [container]
    for qset in sets:
        for item in (qset.get("items") or []) if isinstance(qset, dict) else []:
            if item.get("id") == question_id:
                return item
    for item in items or []:
        if item.get("id") == question_id:
            return item
    raise AssertionError(f"question {question_id} not in vector instance")
