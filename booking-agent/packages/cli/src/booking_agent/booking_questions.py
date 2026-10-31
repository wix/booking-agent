"""Booking questions: condition evaluation and local answer validation (Section 11.4).

Pure functions over the wire dictionaries (``booking_questions.json``). The business
stays authoritative: local validation only lets the agent re-ask before a request,
and every rule here is pinned by the vendored ``eval`` and ``wire`` vectors.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

EMAIL_RE = re.compile(r"^[^\s@<>()\[\]\",;:]+@[^\s@<>()\[\]\",;:]+\.[^\s@<>()\[\]\",;:]+$")
URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)
PHONE_RE = re.compile(r"^\+[1-9]\d{1,14}$")
DURATION_BOUND_RE = re.compile(r"^(-?)P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$")
VALIDITY_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)
SUBMISSION_PATH_RE = re.compile(
    r"^\$(?:\.booking)?\.booking_question_submissions\[\?@\.set_id=='(?P<set>[A-Za-z0-9_.-]+)'\]"
    r"(?:\.answers\['(?P<question>[A-Za-z0-9_.-]+)'\])?$"
)
BUYER_PATH_RE = re.compile(
    r"^\$(?:\.booking)?\.buyer\.(?P<field>first_name|last_name|email|phone_number)$"
)

ORDERED_OPS = {"gt", "gte", "lt", "lte"}

#: Business outcome codes this extension defines (Section 11.4.9).
ANSWER_CODES = {
    "booking_question_answer_missing",
    "booking_question_answer_invalid",
    "booking_question_unknown",
    "booking_questions_changed",
    "booking_question_update_not_allowed",
}
INFO_CODES = {"booking_question_answer_ignored"}
ELIGIBILITY_CODES = {"booking_eligibility_required", "booking_eligibility_expired"}
OUTCOME_CODES = ANSWER_CODES | INFO_CODES | ELIGIBILITY_CODES


# ---------------------------------------------------------------------------
# definitions
# ---------------------------------------------------------------------------

def answer_kind(schema: Mapping[str, Any]) -> str:
    """Coarse answer type of a question's answer schema."""
    if schema.get("type") == "array":
        return "multiple_choice"
    if schema.get("type") == "string" and "oneOf" in schema:
        return "single_choice"
    if schema.get("type") in ("integer", "number"):
        return "number"
    if schema.get("type") == "boolean":
        return "acknowledgement" if schema.get("const") is True else "boolean"
    return {"date": "date", "time": "time", "date-time": "date_time"}.get(
        schema.get("format"), "string"
    )


def options_of(schema: Mapping[str, Any]) -> List[Dict[str, Any]]:
    kind = answer_kind(schema)
    if kind == "multiple_choice":
        return list((schema.get("items") or {}).get("anyOf") or [])
    if kind == "single_choice":
        return list(schema.get("oneOf") or [])
    return []


def condition_leaves(condition: Any) -> Iterator[Dict[str, Any]]:
    if not isinstance(condition, dict):
        return
    if "question" in condition:
        yield condition
    for key in ("all", "any"):
        for member in condition.get(key, []):
            yield from condition_leaves(member)
    if "not" in condition:
        yield from condition_leaves(condition["not"])


def semantic_set_errors(qset: Mapping[str, Any]) -> List[str]:
    """Cross-item rules JSON Schema cannot express, as ``rule:item_id`` strings."""
    errors: List[str] = []
    items = qset.get("items", [])
    seen_ids: set = set()
    questions: Dict[str, Dict[str, Any]] = {}
    index_of: Dict[str, int] = {}
    for i, item in enumerate(items):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            index_of.setdefault(item["id"], i)
    notes = 0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        iid = item.get("id")
        if iid in seen_ids:
            errors.append(f"duplicate_item_id:{iid}")
        seen_ids.add(iid)
        kind = item.get("kind")
        if kind == "section":
            following = items[i + 1] if i + 1 < len(items) else None
            if following is None or following.get("kind") == "section":
                errors.append(f"empty_section:{iid}")
        if kind == "question":
            consts = [o.get("const") for o in options_of(item.get("schema", {}))]
            if len(consts) != len(set(consts)):
                errors.append(f"duplicate_option_const:{iid}")
            if item.get("maps_to") == "notes":
                notes += 1
                if notes == 2:
                    errors.append(f"multiple_maps_to_notes:{iid}")
        conditions = [item.get("when")]
        if isinstance(item.get("required"), dict):
            conditions.append(item["required"])
        for condition in conditions:
            for leaf in condition_leaves(condition):
                ref = leaf.get("question")
                if ref == iid:
                    errors.append(f"self_reference:{iid}")
                elif ref not in index_of:
                    errors.append(f"unknown_reference:{iid}")
                elif index_of[ref] > i:
                    errors.append(f"forward_reference:{iid}")
                elif ref not in questions:
                    errors.append(f"reference_not_question:{iid}")
                else:
                    ref_kind = answer_kind(questions[ref].get("schema", {}))
                    op = leaf.get("op")
                    if op == "contains" and ref_kind != "multiple_choice":
                        errors.append(f"operator_type_mismatch:{iid}")
                    if op in ORDERED_OPS and ref_kind not in ("number", "date", "time", "date_time"):
                        errors.append(f"operator_type_mismatch:{iid}")
        if kind == "question":
            questions[iid] = item
    return errors


MAX_CONDITION_DEPTH = 3


def _condition_depth(condition: Any) -> int:
    if not isinstance(condition, dict) or "question" in condition:
        return 0
    members: List[Any] = list(condition.get("all") or []) + list(condition.get("any") or [])
    if "not" in condition:
        members.append(condition["not"])
    return 1 + max((_condition_depth(m) for m in members), default=0)


def _empty_combinator(condition: Any) -> bool:
    if not isinstance(condition, dict) or "question" in condition:
        return False
    for key in ("all", "any"):
        if key in condition and not condition[key]:
            return True
    members = list(condition.get("all") or []) + list(condition.get("any") or [])
    if "not" in condition:
        members.append(condition["not"])
    return any(_empty_combinator(m) for m in members)


def condition_shape_errors(qset: Mapping[str, Any]) -> List[str]:
    """Condition shapes the schema forbids: combinators nested past depth 3, or empty."""
    errors: List[str] = []
    for item in qset.get("items", []):
        if not isinstance(item, dict):
            continue
        conditions = [item.get("when")]
        if isinstance(item.get("required"), dict):
            conditions.append(item["required"])
        for condition in conditions:
            if condition is None:
                continue
            if _condition_depth(condition) > MAX_CONDITION_DEPTH:
                errors.append(f"condition_depth:{item.get('id')}")
            if _empty_combinator(condition):
                errors.append(f"empty_combinator:{item.get('id')}")
    return errors


def definition_errors(qset: Mapping[str, Any]) -> List[str]:
    """Every reason this client refuses to ask a set (shape and cross-item rules)."""
    return condition_shape_errors(qset) + semantic_set_errors(qset)


def semantic_errors_for(def_name: str, instance: Any) -> List[str]:
    """Semantic errors for any instance that is or carries question sets."""
    if not isinstance(instance, (dict, list)):
        return []
    if def_name == "BookingQuestionSet":
        return semantic_set_errors(instance)
    if def_name == "BookingQuestion" and isinstance(instance, dict):
        return [
            e for e in semantic_set_errors({"items": [instance]})
            if e.startswith("duplicate_option_const:")
        ]
    sets: List[Any] = []
    if def_name == "BookingQuestionSets" and isinstance(instance, list):
        sets = instance
    elif isinstance(instance, dict) and isinstance(instance.get("booking_question_sets"), list):
        sets = instance["booking_question_sets"]
    errors: List[str] = []
    for qset in sets:
        if isinstance(qset, dict):
            errors.extend(semantic_set_errors(qset))
    return errors


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------

def _shift(day: date, sign: int, years: int, months: int, days: int) -> date:
    total = day.year * 12 + day.month - 1 + sign * (years * 12 + months)
    year, month = divmod(total, 12)
    month += 1
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last)) + timedelta(days=sign * days)


def resolve_date_bound(bound: str, request_date: date) -> date:
    """An absolute date, or a duration relative to the request date (business time zone)."""
    if DATE_RE.match(bound):
        return date.fromisoformat(bound)
    match = DURATION_BOUND_RE.match(bound)
    if not match:
        raise ValueError(f"not a date bound: {bound!r}")
    sign = -1 if match.group(1) else 1
    years, months, weeks, days = (int(g or 0) for g in match.groups()[1:])
    return _shift(request_date, sign, years, months, weeks * 7 + days)


def _parse_instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _valid_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return bool(DATE_RE.match(text))


def answer_errors(
    question: Mapping[str, Any], value: Any, request_date: Optional[date] = None,
) -> List[str]:
    """Reasons a present answer fails its question, as short slugs."""
    schema = question.get("schema", {})
    kind = answer_kind(schema)
    if kind == "multiple_choice":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return ["type"]
        errors = []
        options = {o["const"]: o for o in options_of(schema)}
        if any(v not in options for v in value):
            errors.append("option")
        if len(value) != len(set(value)):
            errors.append("unique")
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append("min_items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append("max_items")
        if len(value) > 1 and any(options.get(v, {}).get("exclusive") for v in value):
            errors.append("exclusive")
        return errors
    if kind == "single_choice":
        if not isinstance(value, str):
            return ["type"]
        return [] if value in {o["const"] for o in options_of(schema)} else ["option"]
    if kind in ("boolean", "acknowledgement"):
        if not isinstance(value, bool):
            return ["type"]
        return ["const"] if schema.get("const") is True and value is not True else []
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ["type"]
        if schema.get("type") == "integer" and not float(value).is_integer():
            return ["type"]
        errors = []
        if "minimum" in schema and value < schema["minimum"]:
            errors.append("minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append("maximum")
        return errors
    if not isinstance(value, str):
        return ["type"]
    errors = []
    if "minLength" in schema and len(value) < schema["minLength"]:
        errors.append("min_length")
    if "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append("max_length")
    fmt = schema.get("format")
    if fmt == "email" and not EMAIL_RE.match(value):
        errors.append("format")
    elif fmt == "uri" and not URI_RE.match(value):
        errors.append("format")
    elif fmt == "time" and not TIME_RE.match(value):
        errors.append("format")
    elif fmt == "phone":
        if not PHONE_RE.match(value):
            errors.append("format")
        elif schema.get("allowed_country_codes") and not any(
            value[1:].startswith(code) for code in schema["allowed_country_codes"]
        ):
            errors.append("country_code")
    elif fmt in ("date", "date-time"):
        if fmt == "date" and not _valid_date(value):
            errors.append("format")
        elif fmt == "date-time" and not DATE_TIME_RE.match(value):
            errors.append("format")
        elif request_date is not None:
            day = date.fromisoformat(value[:10])
            if "not_before" in schema and day < resolve_date_bound(schema["not_before"], request_date):
                errors.append("not_before")
            if "not_after" in schema and day > resolve_date_bound(schema["not_after"], request_date):
                errors.append("not_after")
    return errors


# ---------------------------------------------------------------------------
# conditions
# ---------------------------------------------------------------------------

def _ordered(kind: str, value: Any) -> Any:
    if kind == "date_time":
        return _parse_instant(value)
    if kind == "time":
        return value if len(value) == 8 else f"{value}:00"
    return value


def eval_condition(
    condition: Mapping[str, Any], effective: Mapping[str, Any], questions: Mapping[str, Any],
) -> bool:
    """Unanswered or inapplicable references are false; type mismatches are false."""
    if "all" in condition:
        return all(eval_condition(c, effective, questions) for c in condition["all"])
    if "any" in condition:
        return any(eval_condition(c, effective, questions) for c in condition["any"])
    if "not" in condition:
        return not eval_condition(condition["not"], effective, questions)
    ref, op = condition["question"], condition["op"]
    present = ref in effective
    if op == "answered":
        return present
    if not present or ref not in questions:
        return False
    value, operand = effective[ref], condition.get("value")
    kind = answer_kind(questions[ref].get("schema", {}))
    try:
        if op == "equals":
            if kind == "date_time":
                return _parse_instant(value) == _parse_instant(operand)
            return value == operand
        if op == "in":
            return value in operand
        if op == "contains":
            return isinstance(value, list) and operand in value
        left, right = _ordered(kind, value), _ordered(kind, operand)
        return {
            "gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right,
        }[op]
    except (TypeError, ValueError, KeyError):
        return False


def evaluate_set(qset: Mapping[str, Any], answers: Mapping[str, Any]) -> Dict[str, List[str]]:
    """Applicability of every item for one set of answers, in item order."""
    questions: Dict[str, Dict[str, Any]] = {}
    effective: Dict[str, Any] = {}
    applicable: List[str] = []
    required_unanswered: List[str] = []
    pruned: List[str] = []
    section_on = True
    for item in qset.get("items", []):
        kind, iid = item.get("kind"), item.get("id")
        when = item.get("when")
        if kind == "section":
            section_on = when is None or eval_condition(when, effective, questions)
            if section_on:
                applicable.append(iid)
            continue
        on = section_on and (when is None or eval_condition(when, effective, questions))
        if kind == "question":
            questions[iid] = item
        if not on:
            if kind == "question" and iid in answers:
                pruned.append(iid)
            continue
        applicable.append(iid)
        if kind != "question":
            continue
        required = item.get("required")
        if isinstance(required, dict):
            required = eval_condition(required, effective, questions)
        if iid in answers:
            effective[iid] = answers[iid]
        elif required:
            required_unanswered.append(iid)
    known = {i.get("id") for i in qset.get("items", []) if i.get("kind") == "question"}
    return {
        "applicable": applicable,
        "required_unanswered": required_unanswered,
        "pruned": pruned,
        "unknown": [k for k in answers if k not in known],
    }


def is_required(item: Mapping[str, Any], qset: Mapping[str, Any], answers: Mapping[str, Any]) -> bool:
    """Whether an applicable question is required given the earlier answers."""
    required = item.get("required")
    if not isinstance(required, dict):
        return bool(required)
    questions: Dict[str, Any] = {}
    effective: Dict[str, Any] = {}
    for other in qset.get("items", []):
        if other.get("id") == item.get("id"):
            break
        if other.get("kind") == "question":
            questions[other["id"]] = other
            if other["id"] in answers:
                effective[other["id"]] = answers[other["id"]]
    return eval_condition(required, effective, questions)


# ---------------------------------------------------------------------------
# business messages
# ---------------------------------------------------------------------------

def parse_message_path(path: Optional[str]) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """``(kind, set_id, question_id)`` for a booking-questions ``messages[].path``.

    ``kind`` is ``answer`` (question id set), ``set`` (whole set) or ``buyer`` (with
    the buyer field name as ``question_id``). Anything else is None.
    """
    if not path:
        return None
    match = SUBMISSION_PATH_RE.match(path)
    if match:
        question = match.group("question")
        return ("answer" if question else "set", match.group("set"), question)
    match = BUYER_PATH_RE.match(path)
    if match:
        return ("buyer", None, match.group("field"))
    return None


def iso_duration(text: str) -> timedelta:
    match = VALIDITY_RE.match(text)
    if not match:
        raise ValueError(f"not a validity duration: {text!r}")
    y, mo, w, d, h, mi, s = (int(g or 0) for g in match.groups())
    return timedelta(days=y * 365 + mo * 30 + w * 7 + d, hours=h, minutes=mi, seconds=s)


def submissions_digest(submissions: List[Mapping[str, Any]]) -> Optional[str]:
    """Stable digest of what will be sent, or None when nothing is."""
    if not submissions:
        return None
    canonical = json.dumps(
        sorted(submissions, key=lambda s: s.get("set_id", "")),
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plain_text(value: Any) -> Optional[str]:
    """Plain text of a Description object, or a string as is."""
    if isinstance(value, dict):
        return value.get("plain")
    return value if isinstance(value, str) else None
