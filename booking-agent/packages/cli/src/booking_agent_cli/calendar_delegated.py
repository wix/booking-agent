"""Delegated calendar route: the agent's platform writes the event and the CLI records it.

The CLI never holds a token on this route. Each write is a pending request kept in the
event sidecar under the booking's session id, with a one-time ``request_id``. The agent
reports what its platform wrote with ``calendar record-event``; a report that names an
unknown request, or times that differ from the booking, is refused.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .calendar_connection import ROUTE_DELEGATED
from .calendar_event import _load_sidecar, _save_sidecar, build_event_body
from .session import Session

OP_CREATE = "create"
OP_UPDATE = "update"
OP_DELETE = "delete"

RECORD_COMMAND = "booking-agent flow calendar record-event"


def _entry(session_id: str) -> Dict[str, Any]:
    entry = _load_sidecar().get(session_id)
    return entry if isinstance(entry, dict) else {}


def is_delegated(session: Session) -> bool:
    """True when this booking's calendar event belongs to the delegated route.

    An event already recorded keeps the route it was written on, so a later change of
    route on this device never sends a token-created event to the platform or back.
    """
    entry = _entry(session.id)
    if entry.get("event_id") or entry.get("pending"):
        return entry.get("route") == ROUTE_DELEGATED
    return getattr(session, "calendar_route", None) == ROUTE_DELEGATED


def report_commands(op: str, request_id: str) -> Tuple[str, str]:
    """``(done, failed)`` report commands for one request."""
    base = f"{RECORD_COMMAND} --request-id {request_id}"
    if op == OP_DELETE:
        done = base
    else:
        done = (
            f"{base} --event-id <EVENT_ID> --html-link <EVENT_URL> "
            "--start <START_AS_WRITTEN> --end <END_AS_WRITTEN>"
        )
    return done, f"{base} --outcome failed --reason '<what went wrong>'"


def delegated_request(
    session: Session, op: str, *, report_back: bool = True,
) -> Dict[str, Any]:
    """The write for the agent's platform to do, saved as this booking's pending request.

    Idempotent: a create for a booking that already has an event returns that event, and
    a request of the same kind that is still open keeps its ``request_id``. An update with
    no recorded event becomes a create. ``report_back=False`` is for local state that is
    about to be deleted, so there is nothing left to record a report in.
    """
    sidecar = _load_sidecar()
    entry = sidecar.get(session.id)
    entry = entry if isinstance(entry, dict) else {}
    event_id = entry.get("event_id")
    html_link = entry.get("html_link")
    pending = entry.get("pending") if isinstance(entry.get("pending"), dict) else None

    if op == OP_CREATE and event_id:
        return {"status": "recorded", "event_id": event_id, "html_link": html_link}
    if op == OP_UPDATE and not event_id:
        op = OP_CREATE
    if op == OP_DELETE and not event_id and not (pending and pending.get("op") == OP_CREATE):
        return {"status": "skipped", "reason": "no_event"}

    body: Optional[Dict[str, Any]] = None
    if op != OP_DELETE:
        body, err = build_event_body(session)
        if err or not body:
            return {"status": "invalid", "message": err or "invalid event"}

    request_id = (
        pending["request_id"]
        if pending and pending.get("op") == op and pending.get("request_id")
        else f"{session.id}.{secrets.token_hex(6)}"
    )
    request: Dict[str, Any] = {"request_id": request_id, "operation": op}
    if event_id:
        request["event_id"] = event_id
    record: Dict[str, Any] = {"op": op, "request_id": request_id}
    if body is not None:
        start, end = body["start"], body["end"]
        zone = start.get("timeZone")
        event = {
            "summary": body["summary"],
            "description": body["description"],
            "start": start["dateTime"],
            "end": end["dateTime"],
            "time_zone": zone,
        }
        if body.get("location"):
            event["location"] = body["location"]
        request["event"] = event
        record.update({"start": event["start"], "end": event["end"], "time_zone": zone})
    elif not event_id:
        slot = next((s for s in session.slots_presented if s.slot_id == session.slot_id), None)
        if slot is not None:
            request["unreported_event"] = {"start": slot.start, "end": slot.end}

    if report_back:
        done, failed = report_commands(op, request_id)
        request["report_command"] = done
        request["failed_command"] = failed
        entry = {**entry, "route": ROUTE_DELEGATED, "pending": record}
        sidecar[session.id] = entry
        _save_sidecar(sidecar)
    request["status"] = "requested"
    return request


def delegated_instruction(request: Dict[str, Any]) -> str:
    """What the agent tells its platform to do, and what to report back."""
    op = request["operation"]
    event = request.get("event") or {}
    zone = event.get("time_zone")
    when = f"start {event.get('start')} and end {event.get('end')}"
    if zone:
        when += f" (time zone {zone})"
    if op == OP_CREATE:
        text = (
            "Use your platform's own Google Calendar to create one event with exactly the "
            f"fields in data.delegated_request.event: summary, description, location, {when}."
        )
    elif op == OP_UPDATE:
        text = (
            f"Use your platform's own Google Calendar to move event {request.get('event_id')} "
            f"to {when}, and replace its summary, description, and location with "
            "data.delegated_request.event."
        )
    elif request.get("event_id"):
        text = (
            "Use your platform's own Google Calendar to delete event "
            f"{request['event_id']}."
        )
    else:
        unreported = request.get("unreported_event") or {}
        text = (
            "If your platform already created a Google Calendar event for this booking"
            + (f" (starting {unreported['start']})" if unreported.get("start") else "")
            + ", delete it."
        )
    if "report_command" not in request:
        return text + " Nothing needs to be reported back."
    text += f" Then run `{request['report_command']}`"
    if op != OP_DELETE:
        text += (
            " with the event id, the event's link, and the start and end exactly as your "
            "platform wrote them"
        )
    return (
        text + f". If your platform cannot do it, run `{request['failed_command']}`. Never "
        "report an event id or link your platform did not return."
    )


def _find(request_id: str) -> Tuple[Optional[str], Dict[str, Any]]:
    for session_id, entry in _load_sidecar().items():
        if not isinstance(entry, dict):
            continue
        pending = entry.get("pending")
        if isinstance(pending, dict) and pending.get("request_id") == request_id:
            return session_id, entry
    return None, {}


def pending_request(request_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """``(session_id, pending)`` for an open request, or ``(None, None)``."""
    session_id, entry = _find(request_id)
    return (session_id, entry.get("pending")) if session_id else (None, None)


def _instant(value: Optional[str], zone: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        if not zone:
            return None
        try:
            moment = moment.replace(tzinfo=ZoneInfo(zone))
        except (ZoneInfoNotFoundError, ValueError):
            return None
    return moment.astimezone(timezone.utc)


def _http_url(value: Optional[str]) -> Optional[str]:
    url = (value or "").strip()
    return url if url.startswith(("https://", "http://")) else None


def record_delegated_result(
    request_id: str,
    *,
    outcome: str,
    event_id: Optional[str] = None,
    html_link: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply the agent's report for one pending request."""
    session_id, entry = _find(request_id)
    if session_id is None:
        return {"ok": False, "code": "calendar_delegated_request_unknown"}
    pending = entry["pending"]
    op = pending.get("op")
    base = {"session_id": session_id, "operation": op, "request_id": request_id}
    sidecar = _load_sidecar()

    if outcome == "failed":
        cleared = {k: v for k, v in entry.items() if k != "pending"}
        if cleared.get("event_id"):
            sidecar[session_id] = cleared
        else:
            sidecar.pop(session_id, None)
        _save_sidecar(sidecar)
        return {
            "ok": True, "status": "failed", "reason": reason,
            "event_id": entry.get("event_id"), "html_link": entry.get("html_link"), **base,
        }

    if op == OP_DELETE:
        sidecar.pop(session_id, None)
        _save_sidecar(sidecar)
        return {"ok": True, "status": "deleted", "event_id": entry.get("event_id"), **base}

    event_id = (event_id or "").strip()
    link = _http_url(html_link)
    missing = [
        flag for flag, value in (
            ("--event-id", event_id), ("--html-link", link), ("--start", start), ("--end", end),
        ) if not value
    ]
    if missing:
        return {"ok": False, "code": "calendar_delegated_report_incomplete", "missing": missing, **base}

    zone = pending.get("time_zone")
    wanted = (_instant(pending.get("start"), zone), _instant(pending.get("end"), zone))
    written = (_instant(start, zone), _instant(end, zone))
    if None in written or written != wanted:
        return {
            "ok": False,
            "code": "calendar_delegated_time_mismatch",
            "expected": {"start": pending.get("start"), "end": pending.get("end"), "time_zone": zone},
            "reported": {"start": start, "end": end},
            **base,
        }

    sidecar[session_id] = {"route": ROUTE_DELEGATED, "event_id": event_id, "html_link": link}
    _save_sidecar(sidecar)
    return {"ok": True, "status": "recorded", "event_id": event_id, "html_link": link, **base}
