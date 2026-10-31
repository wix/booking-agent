"""Booking questions in the booking flow: what to ask, what was answered, what to send.

All state lives in ``session.booking_questions``. An empty dict (or no active surface)
means the extension is not in use and every function here is a no-op, so a flow that
never negotiated the extension behaves exactly as before.

State keys:

- ``surfaces``: active surfaces (``catalog``, ``availability``, ``bookings``, ``paid_bookings``).
- ``service_id`` and ``sets``: the live definitions (wire dicts) for the chosen service.
- ``required_buyer_fields`` and ``buyer_overrides``: contact fields the business needs and
  the values the user typed for the ones Link did not have.
- ``answers``, ``declined``, ``collected_via``: per set id, per question id.
- ``provided``: per set id, question ids whose sensitive answer was erased after use.
- ``eligibility``: the last decision (token, expiry, required sets).
- ``ineligible``: the service the business refused, with its messages.
- ``reask``: questions the business rejected, with its quoted message.
- ``calendar_choice``: ``all``, ``non_sensitive`` or ``none`` once the user chose.
- ``action``: an outstanding buyer-input Action and the content digest shown.
- ``state``: the business's ``booking_question_state`` entries for the booking.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from booking_agent import booking_questions as bq

PHASE_PREFLIGHT = "preflight"
PHASE_BOOKING = "booking"
_TIMING_FOR_PHASE = {PHASE_PREFLIGHT: "preflight", PHASE_BOOKING: "before_confirmation"}
CALENDAR_CHOICES = ("all", "non_sensitive", "none")
# A preflight token is refreshed this long before it expires.
TOKEN_MARGIN = timedelta(seconds=60)


def state(session: Any) -> Dict[str, Any]:
    if not isinstance(session.booking_questions, dict):
        session.booking_questions = {}
    return session.booking_questions


def surfaces(session: Any) -> List[str]:
    return list(state(session).get("surfaces") or [])


def is_active(session: Any, surface: str = "catalog") -> bool:
    return surface in surfaces(session)


def sets(session: Any) -> List[Dict[str, Any]]:
    if not is_active(session, "catalog"):
        return []
    return list(state(session).get("sets") or [])


# ---------------------------------------------------------------------------
# definitions
# ---------------------------------------------------------------------------

def set_surfaces(session: Any, active: Iterable[str]) -> None:
    """Record activation; switching the extension off forgets everything it held."""
    active = sorted(set(active))
    st = state(session)
    if not active:
        session.booking_questions = {}
        return
    st["surfaces"] = active


def add_surface(session: Any, surface: str) -> None:
    st = state(session)
    if st.get("surfaces"):
        st["surfaces"] = sorted(set(st["surfaces"]) | {surface})


def load_definitions(session: Any, service_id: str, service: Any) -> List[str]:
    """Store the live sets for ``service_id``; return set ids whose revision changed.

    ``service`` is the parsed ``USPService``. Sets this client refuses to ask (shape or
    cross-item errors) are dropped; the business still enforces them and its errors
    come back through the normal handoff. A new service clears every answer.
    """
    st = state(session)
    if not st.get("surfaces"):
        return []
    if st.get("service_id") != service_id:
        surfaces_kept = st.get("surfaces")
        ineligible = st.get("ineligible")
        session.booking_questions = st = {"surfaces": surfaces_kept, "service_id": service_id}
        if ineligible and ineligible.get("service_id") == service_id:
            st["ineligible"] = ineligible
    raw_sets = [s.to_wire() for s in (getattr(service, "booking_question_sets", None) or [])]
    kept = [s for s in raw_sets if not bq.definition_errors(s)]
    changed: List[str] = []
    previous = {s["id"]: s for s in st.get("sets") or []}
    for qset in kept:
        old = previous.get(qset["id"])
        if old is not None and old.get("revision") != qset.get("revision"):
            changed.append(qset["id"])
            _keep_unchanged_answers(st, old, qset)
    for set_id in set(previous) - {s["id"] for s in kept}:
        _forget_set(st, set_id)
    st["sets"] = kept
    fields = getattr(service, "required_buyer_fields", None) or []
    st["required_buyer_fields"] = list(fields)
    return changed


def _items_by_id(qset: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {i.get("id"): i for i in qset.get("items", [])}


def _keep_unchanged_answers(st: Dict[str, Any], old: Dict[str, Any], new: Dict[str, Any]) -> None:
    """After ``booking_questions_changed``: keep answers whose question is unchanged."""
    old_items, new_items = _items_by_id(old), _items_by_id(new)
    set_id = new["id"]
    for key in ("answers", "collected_via"):
        bucket = (st.get(key) or {}).get(set_id) or {}
        for qid in list(bucket):
            if old_items.get(qid) != new_items.get(qid):
                bucket.pop(qid, None)
    declined = (st.get("declined") or {}).get(set_id) or []
    (st.setdefault("declined", {}))[set_id] = [
        q for q in declined if old_items.get(q) == new_items.get(q)
    ]


def _forget_set(st: Dict[str, Any], set_id: str) -> None:
    for key in ("answers", "declined", "collected_via", "provided"):
        (st.get(key) or {}).pop(set_id, None)


def find_set(session: Any, set_id: str) -> Optional[Dict[str, Any]]:
    return next((s for s in sets(session) if s.get("id") == set_id), None)


def find_question(session: Any, question_id: str, set_id: Optional[str] = None):
    for qset in sets(session):
        if set_id and qset.get("id") != set_id:
            continue
        for item in qset.get("items", []):
            if item.get("id") == question_id and item.get("kind") == "question":
                return qset, item
    return None, None


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------

def answers(session: Any, set_id: str) -> Dict[str, Any]:
    return dict((state(session).get("answers") or {}).get(set_id) or {})


def declined(session: Any, set_id: str) -> List[str]:
    return list((state(session).get("declined") or {}).get(set_id) or [])


def provided(session: Any, set_id: str) -> List[str]:
    return list((state(session).get("provided") or {}).get(set_id) or [])


def _answered_view(session: Any, set_id: str) -> Dict[str, Any]:
    """Answers for condition evaluation; an erased sensitive answer still counts as present."""
    view = answers(session, set_id)
    for qid in provided(session, set_id):
        view.setdefault(qid, True)
    return view


def request_date(session: Any) -> date:
    """Today in the business time zone, the anchor for relative date bounds."""
    tz_name = getattr(getattr(session, "business", None), "timezone", None)
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).date() if tz_name else datetime.now(timezone.utc).date()
    except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
        return datetime.now(timezone.utc).date()


def record_answer(session: Any, set_id: str, question_id: str, value: Any) -> List[str]:
    """Validate locally and store; return the failed rules (empty when stored)."""
    qset, item = find_question(session, question_id, set_id)
    if item is None:
        return ["unknown_question"]
    errors = bq.answer_errors(item, value, request_date(session))
    if errors:
        return errors
    st = state(session)
    st.setdefault("answers", {}).setdefault(set_id, {})[question_id] = value
    st.setdefault("collected_via", {}).setdefault(set_id, {})[question_id] = "conversation"
    _drop(st.setdefault("declined", {}).setdefault(set_id, []), question_id)
    _drop((st.get("provided") or {}).get(set_id, []), question_id)
    _clear_reask(st, set_id, question_id)
    _prune(session, qset)
    if qset.get("completion_timing") == "preflight":
        clear_token(session)
    return []


def record_decline(session: Any, set_id: str, question_id: str) -> Optional[str]:
    qset, item = find_question(session, question_id, set_id)
    if item is None:
        return "unknown_question"
    if bq.is_required(item, qset, _answered_view(session, set_id)):
        return "required"
    st = state(session)
    (st.get("answers") or {}).get(set_id, {}).pop(question_id, None)
    decl = st.setdefault("declined", {}).setdefault(set_id, [])
    if question_id not in decl:
        decl.append(question_id)
    _clear_reask(st, set_id, question_id)
    _prune(session, qset)
    return None


def _drop(values: List[str], value: str) -> None:
    if value in values:
        values.remove(value)


def _clear_reask(st: Dict[str, Any], set_id: str, question_id: str) -> None:
    st["reask"] = [
        r for r in st.get("reask") or []
        if not (r.get("set_id") == set_id and r.get("question_id") in (question_id, None))
    ]


def _prune(session: Any, qset: Dict[str, Any]) -> None:
    """Drop answers to questions an earlier answer made inapplicable."""
    st = state(session)
    set_id = qset["id"]
    result = bq.evaluate_set(qset, _answered_view(session, set_id))
    for qid in result["pruned"]:
        for key in ("answers", "collected_via"):
            (st.get(key) or {}).get(set_id, {}).pop(qid, None)
        _drop((st.get("provided") or {}).get(set_id, []), qid)


# ---------------------------------------------------------------------------
# what to ask
# ---------------------------------------------------------------------------

def phase_sets(session: Any, phase: str) -> List[Dict[str, Any]]:
    timing = _TIMING_FOR_PHASE[phase]
    return [s for s in sets(session) if s.get("completion_timing") == timing]


def _askable(item: Dict[str, Any]) -> bool:
    """In-band questions only; platform-surface acknowledgements go through the Action."""
    if item.get("kind") != "question":
        return False
    collection = item.get("collection", "in_band")
    if collection == "business_surface":
        return False
    if collection == "platform_surface" and bq.answer_kind(item.get("schema", {})) == "acknowledgement":
        return False
    return collection in ("in_band", "platform_surface")


def _reask_ids(session: Any, set_id: str) -> Optional[List[Optional[str]]]:
    entries = [r for r in state(session).get("reask") or [] if r.get("set_id") == set_id]
    return [r.get("question_id") for r in entries] if entries else None


def next_question(session: Any, phase: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]]:
    """``(set, question, notices before it)`` for the next question to ask, or None.

    A question the business rejected is asked again even though it has an answer.
    """
    for qset in phase_sets(session, phase):
        set_id = qset["id"]
        view = _answered_view(session, set_id)
        result = bq.evaluate_set(qset, view)
        applicable = set(result["applicable"])
        skipped = set(declined(session, set_id))
        reask = _reask_ids(session, set_id)
        notices: List[Dict[str, Any]] = []
        for item in qset.get("items", []):
            iid = item.get("id")
            if iid not in applicable:
                continue
            if item.get("kind") == "notice":
                notices.append(item)
                continue
            if not _askable(item):
                continue
            redo = reask is not None and (iid in reask or None in reask)
            if redo or (iid not in view and iid not in skipped):
                return qset, item, notices
    return None


def missing_required(session: Any, phase: str) -> List[Tuple[str, str]]:
    """Required, applicable, in-band questions without an answer, as ``(set_id, question_id)``."""
    out: List[Tuple[str, str]] = []
    for qset in phase_sets(session, phase):
        if qset.get("completion_requirement", "required") != "required":
            continue
        result = bq.evaluate_set(qset, _answered_view(session, qset["id"]))
        items = _items_by_id(qset)
        for qid in result["required_unanswered"]:
            if _askable(items.get(qid) or {}):
                out.append((qset["id"], qid))
    for entry in state(session).get("reask") or []:
        if entry.get("set_id") and any(s["id"] == entry["set_id"] for s in phase_sets(session, phase)):
            pair = (entry["set_id"], entry.get("question_id") or "")
            if pair not in out:
                out.append(pair)
    return out


def missing_buyer_fields(session: Any) -> List[str]:
    """Required contact fields neither Link nor the user supplied.

    Buyer contact is never saved to disk, so the check runs while ``buyer resolve`` holds
    the Link values and records only the field names for later commands.
    """
    st = state(session)
    overrides = buyer_overrides(session)
    if getattr(session.buyer, "email", None):
        buyer = buyer_payload_fields(session)
        missing = [f for f in st.get("required_buyer_fields") or [] if not buyer.get(f)]
        st["missing_buyer_fields"] = missing
        return missing
    return [f for f in st.get("missing_buyer_fields") or [] if not overrides.get(f)]


def buyer_payload_fields(session: Any) -> Dict[str, Any]:
    b = session.buyer
    first, _, last = (b.name or "").partition(" ")
    values = {"first_name": first, "last_name": last, "email": b.email, "phone_number": b.phone}
    values.update({k: v for k, v in (state(session).get("buyer_overrides") or {}).items() if v})
    return values


def set_buyer_override(session: Any, field_name: str, value: str) -> None:
    state(session).setdefault("buyer_overrides", {})[field_name] = value


def buyer_overrides(session: Any) -> Dict[str, str]:
    return dict(state(session).get("buyer_overrides") or {})


def notes_question(session: Any) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """The question mapped to core ``notes`` for this service, if any."""
    for qset in sets(session):
        for item in qset.get("items", []):
            if item.get("kind") == "question" and item.get("maps_to") == "notes":
                return qset, item
    return None


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

def preflight_required(session: Any) -> bool:
    return any(
        s.get("completion_requirement", "required") == "required"
        for s in phase_sets(session, PHASE_PREFLIGHT)
    )


def valid_token(session: Any) -> Optional[str]:
    elig = state(session).get("eligibility") or {}
    if elig.get("decision") != "eligible" or not elig.get("token"):
        return None
    expires = elig.get("expires_at")
    if expires:
        try:
            when = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except ValueError:
            return None
        if when - TOKEN_MARGIN <= datetime.now(timezone.utc):
            return None
    return elig["token"]


def record_eligibility(session: Any, result: Any, messages: List[Dict[str, Any]]) -> None:
    st = state(session)
    decision = result.decision
    st["eligibility"] = {
        "decision": decision,
        "token": result.assessment_token,
        "expires_at": result.expires_at,
        "required_sets": [r.model_dump() for r in result.required_sets or []],
        "messages": [m for m in messages if m.get("content")],
        "sets": {s["id"]: s.get("revision") for s in phase_sets(session, PHASE_PREFLIGHT)},
    }
    if decision == "ineligible":
        st["ineligible"] = {
            "service_id": st.get("service_id"),
            "messages": [m.get("content") for m in messages if m.get("content")],
        }
    if decision == "requires_input":
        apply_business_messages(session, messages)


def ineligible(session: Any) -> Optional[Dict[str, Any]]:
    entry = state(session).get("ineligible")
    if entry and entry.get("service_id") == state(session).get("service_id"):
        return entry
    return None


def clear_token(session: Any) -> None:
    state(session).pop("eligibility", None)


# ---------------------------------------------------------------------------
# submissions
# ---------------------------------------------------------------------------

def submissions(session: Any, *, timings: Iterable[str] = ("preflight", "before_confirmation")) -> List[Dict[str, Any]]:
    """Every still-applicable answer, one submission per set (resent in full on UCP Update)."""
    if not is_active(session, "catalog"):
        return []
    wanted = set(timings)
    out: List[Dict[str, Any]] = []
    for qset in sets(session):
        if qset.get("completion_timing") not in wanted:
            continue
        set_id = qset["id"]
        given = answers(session, set_id)
        result = bq.evaluate_set(qset, _answered_view(session, set_id))
        applicable = set(result["applicable"])
        kept = {q: v for q, v in given.items() if q in applicable}
        if not kept:
            continue
        via = {
            q: v for q, v in ((state(session).get("collected_via") or {}).get(set_id) or {}).items()
            if q in kept
        }
        entry: Dict[str, Any] = {"set_id": set_id, "revision": qset["revision"], "answers": kept}
        if via:
            entry["collected_via"] = via
        out.append(entry)
    return out


def submissions_digest(session: Any) -> Optional[str]:
    return bq.submissions_digest(submissions(session))


def request_fields(
    session: Any, *, surface: str, include_token: bool = True, action: bool = False,
) -> Dict[str, Any]:
    """Extra request members for ``surface``, empty when the extension is off there.

    ``action`` tags the sets the outstanding buyer-input Action covers with its id.
    """
    if not is_active(session, surface):
        return {}
    out: Dict[str, Any] = {}
    subs = submissions(session) if surface in ("bookings", "paid_bookings") else []
    entry = state(session).get("action") or {}
    if action and entry.get("id"):
        covered = {i["set_id"] for i in entry.get("items") or []}
        for sub in subs:
            if sub["set_id"] in covered:
                sub["action_id"] = entry["id"]
    if subs:
        out["booking_question_submissions"] = subs
    token = valid_token(session) if include_token else None
    if token:
        out["booking_question_assessment_token"] = token
    return out


# ---------------------------------------------------------------------------
# business messages
# ---------------------------------------------------------------------------

def apply_business_messages(session: Any, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn the business's outcome messages into re-asks. Returns what to tell the agent."""
    st = state(session)
    reask: List[Dict[str, Any]] = []
    buyer_fields: List[str] = []
    changed_sets: List[str] = []
    ignored: List[str] = []
    token_needed = False
    for message in messages:
        code = message.get("code")
        parsed = bq.parse_message_path(message.get("path"))
        if code in bq.ELIGIBILITY_CODES:
            token_needed = True
            continue
        if code == "booking_questions_changed":
            if parsed and parsed[1]:
                changed_sets.append(parsed[1])
            continue
        if code in bq.INFO_CODES:
            if parsed and parsed[2]:
                ignored.append(parsed[2])
            continue
        if parsed is None:
            continue
        kind, set_id, question_id = parsed
        if kind == "buyer":
            buyer_fields.append(question_id)
            continue
        entry = {
            "set_id": set_id, "question_id": question_id if kind == "answer" else None,
            "code": code, "content": message.get("content"),
        }
        if entry not in reask:
            reask.append(entry)
    st["reask"] = reask
    if token_needed:
        clear_token(session)
    for field_name in buyer_fields:
        (st.get("buyer_overrides") or {}).pop(field_name, None)
    if buyer_fields:
        st["missing_buyer_fields"] = buyer_fields
    return {
        "reask": reask, "buyer_fields": buyer_fields, "changed_sets": changed_sets,
        "ignored": ignored, "eligibility_needed": token_needed,
    }


def record_business_state(session: Any, entries: Optional[List[Any]]) -> None:
    if not entries:
        return
    st = state(session)
    if not st.get("surfaces"):
        return
    st["state"] = [
        {k: v for k, v in (e.model_dump() if hasattr(e, "model_dump") else dict(e)).items()
         if v is not None and k != "answers"}
        for e in entries
    ]


def state_view(session: Any) -> List[Dict[str, Any]]:
    """The business's set states for status output (answers never included)."""
    titles = {s["id"]: bq.plain_text(s.get("title")) for s in sets(session)}
    out = []
    for entry in state(session).get("state") or []:
        row = dict(entry)
        if titles.get(row.get("set_id")):
            row["title"] = titles[row["set_id"]]
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# what the user sees
# ---------------------------------------------------------------------------

def _option_title(item: Dict[str, Any], const: str) -> str:
    for option in bq.options_of(item.get("schema", {})):
        if option.get("const") == const:
            return option.get("title") or const
    return const


def answer_text(item: Dict[str, Any], value: Any) -> str:
    kind = bq.answer_kind(item.get("schema", {}))
    if kind == "single_choice":
        return _option_title(item, value)
    if kind == "multiple_choice":
        return ", ".join(_option_title(item, v) for v in value)
    if kind in ("boolean", "acknowledgement"):
        return "Yes" if value else "No"
    return str(value)


def answered_items(session: Any, *, include_sensitive: bool = True) -> List[Tuple[Dict[str, Any], Dict[str, Any], Any]]:
    """``(set, question, value)`` for every applicable answer, ``value`` None when erased."""
    out = []
    for qset in sets(session):
        set_id = qset["id"]
        given = answers(session, set_id)
        gone = set(provided(session, set_id))
        applicable = set(bq.evaluate_set(qset, _answered_view(session, set_id))["applicable"])
        for item in qset.get("items", []):
            qid = item.get("id")
            if item.get("kind") != "question" or qid not in applicable:
                continue
            if item.get("sensitive") and not include_sensitive:
                continue
            if qid in given:
                out.append((qset, item, given[qid]))
            elif qid in gone:
                out.append((qset, item, None))
    return out


def recap_lines(session: Any) -> List[str]:
    """One line per answer. Sensitive answers show only that they were provided."""
    lines = []
    for _qset, item, value in answered_items(session):
        label = bq.plain_text(item.get("label")) or item.get("id")
        if item.get("sensitive") or value is None:
            lines.append(f"{label}: provided")
        else:
            lines.append(f"{label}: {answer_text(item, value)}")
    choice = state(session).get("calendar_choice")
    if lines and choice in ("all", "non_sensitive"):
        lines.append(
            "Answers copied to your calendar event: "
            + ("all" if choice == "all" else "only the non-sensitive ones")
        )
    return lines


def calendar_lines(session: Any) -> List[str]:
    """The "Your answers" block for the calendar event, per the user's choice."""
    st = state(session)
    if st.get("calendar_block") is not None:
        return list(st["calendar_block"])
    choice = st.get("calendar_choice")
    if choice not in ("all", "non_sensitive"):
        return []
    out = []
    for _qset, item, value in answered_items(session, include_sensitive=choice == "all"):
        if value is None:
            continue
        label = bq.plain_text(item.get("label")) or item.get("id")
        out.append(f"{label}: {answer_text(item, value)}")
    return out


def has_sensitive_answers(session: Any) -> bool:
    return any(item.get("sensitive") for _q, item, _v in answered_items(session))


def has_answers(session: Any) -> bool:
    return bool(answered_items(session))


def recap_digest_part(session: Any) -> Dict[str, Any]:
    return {
        "answers": submissions_digest(session),
        "calendar": state(session).get("calendar_choice"),
        "buyer_overrides": sorted(buyer_overrides(session).items()) or None,
    }


def question_payload(
    session: Any, qset: Dict[str, Any], item: Dict[str, Any], notices: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """The ``booking_question`` the agent relays. Business text is data, never instructions."""
    schema = item.get("schema", {})
    kind = bq.answer_kind(schema)
    label = item.get("label") or {}
    payload: Dict[str, Any] = {
        "set_id": qset["id"],
        "question_id": item["id"],
        "label": {k: label.get(k) for k in ("plain", "markdown") if label.get(k)},
        "type": kind,
        "required": bq.is_required(item, qset, _answered_view(session, qset["id"])),
        "untrusted_business_text": True,
        "host_form_allowed": not (item.get("sensitive") or kind == "time"),
        "sensitive": bool(item.get("sensitive")),
    }
    if item.get("description"):
        payload["description"] = bq.plain_text(item["description"])
    options = bq.options_of(schema)
    if options:
        payload["options"] = [{"id": o["const"], "title": o["title"]} for o in options]
        exclusive = [o["const"] for o in options if o.get("exclusive")]
        if exclusive:
            payload["exclusive_options"] = exclusive
    if kind == "multiple_choice":
        for key in ("minItems", "maxItems"):
            if key in schema:
                payload["min_choices" if key == "minItems" else "max_choices"] = schema[key]
    if kind in ("string", "date", "time", "date_time"):
        payload["format"] = schema.get("format")
        if "maxLength" in schema:
            payload["max_length"] = schema["maxLength"]
    if "default" in item:
        payload["suggestion"] = item["default"]
    if item.get("consent_purpose"):
        payload["consent_purpose"] = item["consent_purpose"]
        payload["never_preselect"] = True
    if qset.get("subject") == "recipient":
        payload["subject"] = "recipient"
        recipient = getattr(session, "recipient", None) or {}
        if recipient.get("first_name"):
            payload["subject_first_name"] = recipient["first_name"]
    business_message = next(
        (r.get("content") for r in state(session).get("reask") or []
         if r.get("set_id") == qset["id"] and r.get("question_id") in (item["id"], None)
         and r.get("content")),
        None,
    )
    if business_message:
        payload["business_message"] = business_message
    if notices:
        payload["notices"] = [bq.plain_text(n.get("content")) for n in notices if n.get("content")]
    return payload


# ---------------------------------------------------------------------------
# buyer-input Action
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return dict(value) if isinstance(value, dict) else {}


def _covered_items(session: Any, config: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """The exact items the Action covers, or None when any of them is unknown here."""
    wanted = set(config.get("item_ids") or [])
    out: List[Dict[str, Any]] = []
    for ref in config.get("sets") or []:
        qset = find_set(session, ref.get("set_id"))
        if qset is None or qset.get("revision") != ref.get("revision"):
            return None
        view = _answered_view(session, qset["id"])
        applicable = set(bq.evaluate_set(qset, view)["applicable"])
        for item in qset.get("items", []):
            iid = item.get("id")
            if item.get("kind") != "question" or (wanted and iid not in wanted):
                continue
            if not wanted and iid not in applicable:
                continue
            label = item.get("label") or {}
            out.append({
                "set_id": qset["id"], "revision": qset["revision"], "question_id": iid,
                "label": label.get("plain") or bq.plain_text(label),
                "kind": bq.answer_kind(item.get("schema", {})),
            })
    found = {i["question_id"] for i in out}
    if wanted - found:
        return None
    return out


def record_action(
    session: Any, actions: Any, *, surface: str, booking_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Remember the outstanding buyer-input Action, or forget it when none is pending.

    ``actions`` is a Booking's ``actions`` list, or the UCP actions-map array. The
    Action is answerable here only in ``acknowledge`` mode over acknowledgement items
    this client has the exact definitions for; anything else goes to ``continue_url``.
    """
    from booking_agent.namespace import ACTION_BOOKING_QUESTIONS_BUYER_INPUT

    st = state(session)
    if not st.get("surfaces"):
        return None
    for raw in actions or []:
        action = _as_dict(raw)
        kind = action.get("type")
        if kind not in (None, ACTION_BOOKING_QUESTIONS_BUYER_INPUT):
            continue
        if action.get("status") not in (None, "pending"):
            continue
        config = action.get("config") or {}
        if not action.get("id") or not config:
            continue
        items = _covered_items(session, config)
        processable = bool(
            is_active(session, surface)
            and items
            and config.get("mode") == "acknowledge"
            and config.get("presentation") == "verbatim"
            and all(i["kind"] == "acknowledgement" for i in items)
        )
        entry = {
            "id": action["id"],
            "mode": config.get("mode"),
            "surface": surface,
            "booking_id": booking_id,
            "continue_url": action.get("continue_url"),
            "items": items or [],
            "processable": processable,
            "content_digest": content_digest({"id": action["id"], "items": items or []}),
        }
        st["action"] = entry
        return entry
    st.pop("action", None)
    return None


def action_submissions(session: Any) -> List[Dict[str, Any]]:
    """The explicit acknowledgements for the outstanding Action, one submission per set."""
    entry = state(session).get("action") or {}
    by_set: Dict[str, Dict[str, Any]] = {}
    for item in entry.get("items") or []:
        sub = by_set.setdefault(item["set_id"], {
            "set_id": item["set_id"], "revision": item["revision"], "answers": {},
            "action_id": entry["id"], "collected_via": {},
        })
        sub["answers"][item["question_id"]] = True
        sub["collected_via"][item["question_id"]] = "platform_surface"
    return list(by_set.values())


def record_action_answers(session: Any) -> None:
    st = state(session)
    for sub in action_submissions(session):
        st.setdefault("answers", {}).setdefault(sub["set_id"], {}).update(sub["answers"])
        st.setdefault("collected_via", {}).setdefault(sub["set_id"], {}).update(sub["collected_via"])


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------

def erase_sensitive(session: Any) -> None:
    """Erase sensitive answer values, keeping a ``provided`` marker for each."""
    st = state(session)
    for qset in sets(session):
        set_id = qset["id"]
        bucket = (st.get("answers") or {}).get(set_id) or {}
        for item in qset.get("items", []):
            qid = item.get("id")
            if item.get("sensitive") and qid in bucket:
                bucket.pop(qid)
                marks = st.setdefault("provided", {}).setdefault(set_id, [])
                if qid not in marks:
                    marks.append(qid)


def retain_after_confirmation(session: Any) -> None:
    """Once the business confirmed, keep no sensitive answer or typed contact detail.

    The calendar block is frozen first when the user chose to copy answers into their
    event, so later event updates keep the same description.
    """
    st = state(session)
    if not st.get("surfaces"):
        return
    if st.get("calendar_choice") in ("all", "non_sensitive") and "calendar_block" not in st:
        st["calendar_block"] = calendar_lines(session)
    erase_sensitive(session)
    for key in ("buyer_overrides", "missing_buyer_fields", "eligibility", "action", "reask"):
        st.pop(key, None)


def erase_all(session: Any) -> None:
    """Forget every answer, override and token; keep activation and definitions."""
    st = state(session)
    for key in ("answers", "declined", "collected_via", "provided", "eligibility",
                "buyer_overrides", "reask", "calendar_choice", "calendar_offered",
                "action", "missing_buyer_fields"):
        st.pop(key, None)


def content_digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
