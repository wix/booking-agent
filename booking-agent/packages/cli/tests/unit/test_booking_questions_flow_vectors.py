"""Every vendored booking-question flow vector, recomputed with this client's rules.

Wire and eval vectors already run in ``test_booking_questions_vectors``. A flow
vector asserts the messages a submission produces. Those messages are derived
here from ``evaluate_set`` and ``answer_errors``, in the order Section 11.4.9
fixes, and the composed phone, text, and acknowledgement vector is expanded
and checked the same way. A change to the client's applicability or answer
rules fails the vector it disagrees with.
"""
from __future__ import annotations

import copy
import itertools
import json
from datetime import datetime
from pathlib import Path

import pytest

from booking_agent import booking_questions as bq
from booking_agent.models.booking_questions import BookingQuestionSubmission

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "booking_questions"
FLOW = VECTORS / "flow"
FIXTURES = VECTORS / "fixtures"

DERIVED_CODES = {
    "booking_question_answer_missing",
    "booking_question_answer_invalid",
    "booking_question_unknown",
    "booking_questions_changed",
    "booking_question_answer_ignored",
}
PHASE_OF_STEP = {
    "check_eligibility": "preflight",
    "create_booking": "before_confirmation",
    "create_checkout": "before_confirmation",
    "update_booking": None,
    "update_checkout": None,
}
_OMIT = object()
_MAX_USE_DEPTH = 8


def _load_flow():
    docs = []
    for path in sorted(FLOW.glob("bq-*.json")):
        vector = json.loads(path.read_text())
        if vector.get("kind") != "flow":
            continue
        docs.append(vector)
        for index, variant in enumerate(vector.get("variants") or []):
            if "given" in variant or "when" in variant:
                merged = {**vector, **variant}
                merged["id"] = f"{vector['id']}[{variant.get('case', index)}]"
                docs.append(merged)
    return docs


def _steps(case):
    when = case.get("when")
    if isinstance(when, dict):
        when = when.get("steps", [])
    return [step for step in (when or []) if isinstance(step, dict)]


def _service(case):
    service = (case.get("given") or {}).get("service")
    return service if isinstance(service, dict) else {}


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def submitted_values(submission):
    """Question id to typed value. A bare value is not a submission."""
    answers = submission.get("answers") or {}
    if not isinstance(answers, dict):
        raise ValueError("submission answers is not an object of question id to answer entry")
    values = {}
    for qid, entry in answers.items():
        if not isinstance(entry, dict) or "value" not in entry:
            raise ValueError(f"answer {qid!r} must be an entry with value")
        values[qid] = entry["value"]
    return values


def _set_path(set_id):
    return f"$.booking_question_submissions[?@.set_id=='{set_id}']"


def _question_path(set_id, qid):
    return f"{_set_path(set_id)}.answers['{qid}']"


def _instant(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def derive_messages(service, step, history=None):
    """``(code, path)`` pairs this client's rules derive for one request."""
    name = step.get("step")
    phase = PHASE_OF_STEP.get(name)
    creating = name in ("create_booking", "create_checkout", "check_eligibility")
    body = step.get("with") or {}
    at = step.get("at", "")
    request_date = None
    if bq.DATE_RE.match(at[:10]):
        from datetime import date
        request_date = date.fromisoformat(at[:10])
    out = []

    if name in ("create_booking", "create_checkout"):
        buyer = body.get("buyer") or {}
        for member in service.get("required_buyer_fields") or []:
            if not buyer.get(member):
                out.append(("booking_question_answer_missing", f"$.buyer.{member}"))

    submissions = {
        item.get("set_id"): item
        for item in body.get("booking_question_submissions") or []
        if isinstance(item, dict)
    }
    prior = {item.get("set_id"): item for item in (history or [])}
    for qset in service.get("booking_question_sets") or []:
        sid = qset["id"]
        sub = submissions.get(sid)
        in_phase = creating and qset.get("completion_timing") == phase
        required_set = qset.get("completion_requirement", "required") == "required"
        if sub is not None and sub.get("revision") != qset.get("revision"):
            out.append(("booking_questions_changed", _set_path(sid)))
            continue
        if sub is None:
            if not (in_phase and required_set):
                continue
            done = prior.get(sid)
            if done and qset.get("frequency") != "every_booking":
                if done.get("revision") != qset.get("revision"):
                    out.append(("booking_questions_changed", _set_path(sid)))
                    continue
                validity = qset.get("validity")
                if validity is None or (
                    at and _instant(done["completed_at"]) + bq.iso_duration(validity) > _instant(at)
                ):
                    continue
            answers = {}
        else:
            answers = submitted_values(sub)

        acting = sub is not None and "action_id" in sub
        by_id = {item.get("id"): item for item in qset.get("items") or []}
        for qid in list(answers):
            item = by_id.get(qid)
            if (
                item and item.get("kind") == "question"
                and item.get("collection") == "platform_surface" and not acting
            ):
                del answers[qid]
        result = bq.evaluate_set(qset, answers)
        missing = set(result["required_unanswered"]) if (in_phase and required_set) else set()
        for item in qset.get("items") or []:
            qid = item.get("id")
            if item.get("kind") != "question" or qid not in result["applicable"]:
                continue
            if qid in answers:
                if bq.answer_errors(item, answers[qid], request_date):
                    out.append(("booking_question_answer_invalid", _question_path(sid, qid)))
            elif qid in missing and item.get("collection", "in_band") == "in_band":
                out.append(("booking_question_answer_missing", _question_path(sid, qid)))
        for qid in result["unknown"]:
            out.append(("booking_question_unknown", _question_path(sid, qid)))
        for qid in result["pruned"]:
            out.append(("booking_question_answer_ignored", _question_path(sid, qid)))
    return out


def asserted_derived(step):
    return [
        (message.get("code"), message.get("path"))
        for message in (step.get("result") or {}).get("messages") or []
        if isinstance(message, dict) and message.get("code") in DERIVED_CODES and not message.get("policy")
    ]


def _flow_params():
    return [pytest.param(vector, id=vector["id"]) for vector in _load_flow()]


@pytest.mark.parametrize("vector", _flow_params())
def test_flow_vector_messages_match_the_clients_rules(vector):
    service = _service(vector)
    for qset in service.get("booking_question_sets") or []:
        assert bq.semantic_set_errors(qset) == [], vector["id"]
    # A platform that did not negotiate the extension is not offered its questions,
    # and the business must not answer it with this extension's codes.
    if (vector.get("given") or {}).get("extension_negotiated") is False:
        for step in _steps(vector):
            codes = [
                message.get("code") for message in (step.get("result") or {}).get("messages") or []
                if isinstance(message, dict)
            ]
            assert bq.OUTCOME_CODES.isdisjoint(codes), vector["id"]
        return
    history = ((vector.get("given") or {}).get("buyer_history") or {}).get("completed_sets")
    if not (service.get("booking_question_sets") or service.get("required_buyer_fields")):
        return
    for index, step in enumerate(_steps(vector)):
        if step.get("step") not in PHASE_OF_STEP or "result" not in step or step.get("derive") is False:
            continue
        if "booking_question_submissions" not in (step.get("with") or {}) and step.get("step") not in (
            "create_booking", "check_eligibility",
        ):
            continue
        derived = derive_messages(service, step, history)
        assert derived == asserted_derived(step), (vector["id"], index, step.get("step"))


@pytest.mark.parametrize("vector", _flow_params())
def test_flow_vector_submissions_use_answer_entries(vector):
    for step in _steps(vector):
        for submission in (step.get("with") or {}).get("booking_question_submissions") or []:
            BookingQuestionSubmission.model_validate(submission)
            submitted_values(submission)


@pytest.mark.parametrize("vector", _flow_params())
def test_flow_vector_results_do_not_echo_sensitive_answers(vector):
    sensitive = {
        item["id"]
        for qset in _service(vector).get("booking_question_sets") or []
        for item in qset.get("items") or []
        if item.get("kind") == "question" and item.get("sensitive")
    }
    secrets = set()
    for step in _steps(vector):
        for submission in (step.get("with") or {}).get("booking_question_submissions") or []:
            for qid, value in submitted_values(submission).items():
                if qid in sensitive:
                    secrets.update(text for text in _strings(value) if len(text) >= 3)
    rendered = json.dumps([vector.get("then")] + [step.get("result") for step in _steps(vector)])
    leaked = sorted(secret for secret in secrets if secret in rendered)
    assert leaked == [], vector["id"]


# ---------------------------------------------------------------------------
# composition (bq-flow-phone-text-ack-combined)
# ---------------------------------------------------------------------------

class _ComposeError(Exception):
    pass


def _pointer_get(doc, pointer):
    node = doc
    for token in [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]:
        node = node[int(token)] if isinstance(node, list) else node[token]
    return node


def _apply_patch(doc, operations):
    doc = copy.deepcopy(doc)
    for op in operations:
        kind, path = op.get("op"), op.get("path", "")
        tokens = [part.replace("~1", "/").replace("~0", "~") for part in path.split("/")[1:]]
        if kind == "test":
            if _pointer_get(doc, path) != op.get("value"):
                raise _ComposeError(f"JSON Patch test failed at {path}")
            continue
        if kind in ("copy", "move"):
            value = copy.deepcopy(_pointer_get(doc, op["from"]))
            if kind == "move":
                doc = _apply_patch(doc, [{"op": "remove", "path": op["from"]}])
            doc = _apply_patch(doc, [{"op": "add", "path": path, "value": value}])
            continue
        if not tokens:
            if kind in ("add", "replace"):
                doc = copy.deepcopy(op["value"])
                continue
            raise _ComposeError("cannot remove the document root")
        parent_pointer = "/" + "/".join(
            token.replace("~", "~0").replace("/", "~1") for token in tokens[:-1]
        )
        parent = _pointer_get(doc, parent_pointer) if tokens[:-1] else doc
        last = tokens[-1]
        try:
            if isinstance(parent, list):
                if kind == "add":
                    parent.insert(len(parent) if last == "-" else int(last), copy.deepcopy(op["value"]))
                elif kind == "remove":
                    del parent[int(last)]
                elif kind == "replace":
                    parent[int(last)] = copy.deepcopy(op["value"])
                else:
                    raise _ComposeError(f"unsupported JSON Patch op {kind!r}")
            else:
                if kind == "add":
                    parent[last] = copy.deepcopy(op["value"])
                elif kind in ("remove", "replace"):
                    if last not in parent:
                        raise _ComposeError(f"JSON Patch {kind} target {path} does not exist")
                    if kind == "remove":
                        del parent[last]
                    else:
                        parent[last] = copy.deepcopy(op["value"])
                else:
                    raise _ComposeError(f"unsupported JSON Patch op {kind!r}")
        except (IndexError, KeyError, ValueError) as exc:
            raise _ComposeError(f"JSON Patch {kind} at {path} failed: {exc}") from exc
    return doc


def _merge_patch(target, patch):
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def _apply_edits(doc, edits):
    for edit in edits or []:
        doc = _merge_patch(doc, edit["merge"]) if "merge" in edit else _apply_patch(doc, [edit])
    return doc


def _load_fixtures():
    fixtures = {}
    for path in sorted(FIXTURES.glob("*.json")):
        doc = json.loads(path.read_text())
        fixtures[doc.get("id", path.stem)] = doc
    return fixtures


def _load_vectors():
    out = {}
    for path in sorted(VECTORS.rglob("bq-*.json")):
        if FIXTURES in path.parents:
            continue
        doc = json.loads(path.read_text())
        out[doc.get("id", path.stem)] = doc
    return out


def _resolve(node, fixtures, vectors, params=None, stack=()):
    if len(stack) > _MAX_USE_DEPTH:
        raise _ComposeError(f"$use nesting deeper than {_MAX_USE_DEPTH}: {' > '.join(stack)}")
    if isinstance(node, list):
        return [
            value for item in node
            if (value := _resolve(item, fixtures, vectors, params, stack)) is not _OMIT
        ]
    if not isinstance(node, dict):
        return node
    if set(node) == {"$param"}:
        name = node["$param"]
        if params is None or name not in params:
            raise _ComposeError(f"$param {name!r} used outside a fixture that declares it")
        return params[name]
    if "$use" in node:
        ref = node["$use"]
        bound = {
            key: _resolve(value, fixtures, vectors, params, stack)
            for key, value in (node.get("with") or {}).items()
        }
        extra = {key: value for key, value in node.items() if key not in ("$use", "with")}
        if ref in stack:
            raise _ComposeError(f"$use cycle: {' > '.join(stack + (ref,))}")
        if ref.startswith("vector:"):
            vid, _, pointer = ref[len("vector:"):].partition("#")
            if vid not in vectors:
                raise _ComposeError(f"unresolved $use {ref!r}")
            body = copy.deepcopy(_pointer_get(vectors[vid], pointer))
            value = _resolve(body, fixtures, vectors, None, stack + (ref,))
        else:
            fixture = fixtures.get(ref)
            if fixture is None:
                raise _ComposeError(f"unresolved $use {ref!r}")
            declared = fixture.get("parameters") or {}
            for name in bound:
                if name not in declared:
                    raise _ComposeError(f"$use {ref!r} binds unknown parameter {name!r}")
            values = {}
            for name, spec in declared.items():
                if name in bound:
                    values[name] = bound[name]
                elif spec.get("required"):
                    raise _ComposeError(f"$use {ref!r} is missing required parameter {name!r}")
                elif spec.get("optional"):
                    values[name] = _OMIT
                else:
                    values[name] = copy.deepcopy(spec.get("default"))
            value = _resolve(
                copy.deepcopy(fixture["body"]), fixtures, vectors, values, stack + (ref,),
            )
        if extra:
            if not isinstance(value, dict):
                raise _ComposeError(f"$use {ref!r} with sibling members must inline an object")
            value = {**value, **_resolve(extra, fixtures, vectors, params, stack)}
        return value
    return {
        key: resolved
        for key, item in node.items()
        if (resolved := _resolve(item, fixtures, vectors, params, stack)) is not _OMIT
    }


def _assignment_key(assignment):
    return ",".join(f"{factor}={level}" for factor, level in assignment)


def _derive_case(compose, steps, assignment):
    factors = compose["factors"]
    present = {step.get("stage") for step in steps}
    stages = [stage for stage in compose["stages"] if stage["name"] in present]
    derived = {"rejected": False, "messages": [], "unreached": []}
    extra_then = {}
    for index, stage in enumerate(stages):
        rejects, results = [], []
        for factor, level in assignment:
            for effect in factors[factor][level].get("effects") or []:
                if effect.get("stage") != stage["name"]:
                    continue
                if "reject" in effect:
                    rejects.append(effect["reject"])
                if "result" in effect:
                    results.append(effect["result"])
                extra_then.update(effect.get("then") or {})
        derived["final_stage"] = stage["name"]
        if rejects:
            derived["rejected"] = True
            derived["rejections"] = rejects
            derived["messages"] = [reject["path"] for reject in rejects]
            derived["unreached"] = [item["name"] for item in stages[index + 1:]]
            derived.pop("final_status", None)
            break
        if len({json.dumps(result, sort_keys=True) for result in results}) > 1:
            raise _ComposeError(f"conflicting results for stage {stage['name']!r}")
        result = results[-1] if results else stage.get("default") or {}
        for key, value in result.items():
            derived["final_status" if key == "status" else key] = value
    derived.update(extra_then)
    create_rejected = derived["rejected"] and derived.get("final_stage") == stages[0]["name"]
    derived.setdefault("booking_created", not create_rejected)
    derived.setdefault("hold_changed", False if create_rejected else None)
    derived.setdefault("payment_authorized", False)
    if derived["hold_changed"] is None:
        del derived["hold_changed"]
    return derived


def _invariant_problem(name, case, derived):
    steps = _steps(case)
    create_rejected = derived["rejected"] and derived.get("final_stage") == (
        steps[0].get("stage") if steps else None
    )
    if name == "no_booking_resource_when_create_rejected":
        if create_rejected and derived.get("booking_created") is not False:
            return "a rejected create still reports a booking resource"
    elif name == "hold_expires_at_and_capacity_unchanged_when_create_rejected":
        if create_rejected and derived.get("hold_changed") is not False:
            return "a rejected create changes the hold"
    elif name == "no_payment_authorized_or_captured_before_confirmed":
        if derived.get("payment_authorized") and derived.get("final_status") != "confirmed":
            return "payment is authorized before the booking is confirmed"
    elif name == "bare_acknowledgement_never_resolves_action":
        surface = {
            item["id"]
            for qset in _service(case).get("booking_question_sets") or []
            for item in qset.get("items") or []
            if item.get("kind") == "question" and item.get("collection") == "platform_surface"
        }
        bare, acted = set(), set()
        for step in steps:
            for submission in (step.get("with") or {}).get("booking_question_submissions") or []:
                hit = surface & set(submission.get("answers") or {})
                (acted if "action_id" in submission else bare).update(hit)
        if (bare - acted) and (
            derived.get("final_status") == "confirmed" or derived.get("acceptance_recorded") is True
        ):
            return "a bare acknowledgement without an Action id resolved the Action"
    elif name == "messages_never_echo_answer_values":
        values = set()
        for step in steps:
            for submission in (step.get("with") or {}).get("booking_question_submissions") or []:
                values.update(text for text in _strings(submitted_values(submission)) if len(text) >= 3)
        rendered = json.dumps(derived.get("rejections") or [])
        if any(value in rendered for value in values):
            return "a message echoes a submitted answer value"
    else:
        return f"unknown invariant {name!r}"
    return None


def expand_compose(vector):
    """Expanded flow cases and composition problems for one compose vector."""
    compose = vector.get("compose") or {}
    problems = []
    factors = compose.get("factors") or {}
    names = list(factors)
    excluded = {item["case"] for item in compose.get("exclude") or []}
    for item in compose.get("exclude") or []:
        if not item.get("reason"):
            problems.append(f"exclusion {item.get('case')!r} has no written reason")
    combos = [
        list(zip(names, levels))
        for levels in itertools.product(*(list(factors[name]) for name in names))
    ]
    combos = [combo for combo in combos if _assignment_key(combo) not in excluded]
    limit = compose.get("max_cases", 64)
    if len(combos) > limit:
        return [], [f"{len(combos)} generated cases exceed max_cases {limit}"]

    fixtures, vectors = _load_fixtures(), _load_vectors()
    cases = []
    for assignment in combos:
        key = _assignment_key(assignment)
        case_id = f"{vector['id']}[{key}]"
        try:
            raw = copy.deepcopy(compose["base"])
            for factor, level in assignment:
                raw = _apply_edits(raw, factors[factor][level].get("apply"))
            expanded = _resolve(raw, fixtures, vectors)
            steps = _steps(expanded)
            derived = _derive_case(compose, steps, assignment)
        except _ComposeError as exc:
            problems.append(f"{case_id}: {exc}")
            continue
        rejected_stage = derived.get("final_stage") if derived["rejected"] else None
        for step in steps:
            if step.get("stage") == rejected_stage:
                step["result"] = {"messages": [
                    {
                        "type": "error", "code": reject["outcome_code"],
                        "content": reject.get("content", f"{reject['outcome_code']} at {reject['path']}"),
                        "severity": "recoverable", "path": reject["path"],
                    }
                    for reject in derived.get("rejections") or []
                ]}
            elif step.get("stage") in derived["unreached"]:
                step["unreached"] = True
            else:
                step["result"] = {"messages": []}
        expanded["when"] = {"steps": [step for step in steps if not step.get("unreached")]}
        expanded["when"]["unreached"] = [step.get("stage") for step in steps if step.get("unreached")]
        derived.pop("rejections", None)
        expanded["then"] = derived
        expanded = {"id": case_id, "kind": "flow", **expanded}
        for name in compose.get("invariants") or []:
            problem = _invariant_problem(name, expanded, derived)
            if problem:
                problems.append(f"{case_id}: invariant {name}: {problem}")
        cases.append((key, expanded))

    anchors = compose.get("anchors") or {}
    by_key = dict(cases)
    for key, expected in anchors.items():
        if key not in by_key:
            problems.append(f"anchor {key!r} names no generated case")
            continue
        got = by_key[key]["then"]
        for member, value in expected.items():
            if got.get(member) != value:
                problems.append(
                    f"anchor {key!r}: {member} is {got.get(member)!r}, anchor expects {value!r}"
                )
    if names:
        baseline = [(name, next(iter(factors[name]))) for name in names]
        required = {_assignment_key(baseline)}
        for index, name in enumerate(names):
            for level in list(factors[name])[1:]:
                deviation = list(baseline)
                deviation[index] = (name, level)
                required.add(_assignment_key(deviation))
        for key in sorted(required - set(anchors) - excluded):
            problems.append(f"case {key!r} must be anchored (all-pass or single-factor deviation)")
    return [case for _, case in cases], problems


def test_composed_phone_text_acknowledgement_cases_match_the_clients_rules():
    vector = json.loads((FLOW / "bq-flow-phone-text-ack-combined.json").read_text())
    cases, problems = expand_compose(vector)
    assert problems == []
    assert len(cases) == 12
    for case in cases:
        service = _service(case)
        history = ((case.get("given") or {}).get("buyer_history") or {}).get("completed_sets")
        for qset in service.get("booking_question_sets") or []:
            assert bq.semantic_set_errors(qset) == [], case["id"]
        for step in _steps(case):
            for submission in (step.get("with") or {}).get("booking_question_submissions") or []:
                BookingQuestionSubmission.model_validate(submission)
            if step.get("step") not in PHASE_OF_STEP or "result" not in step:
                continue
            if "booking_question_submissions" not in (step.get("with") or {}) and step.get("step") not in (
                "create_booking", "check_eligibility",
            ):
                continue
            assert derive_messages(service, step, history) == asserted_derived(step), case["id"]
