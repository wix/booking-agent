"""Consented support report (USP spec issue #307).

When a booking fails in a way the agent cannot recover from, ``emit_error`` offers
the buyer a one-time note to the team that looks after the booking assistant. The
CLI builds the note, freezes its bytes on the session, and is the only thing that
can send them, to one baked-in ingest URL, and only with ``--user-confirmed``.

Every value in the note is projected from an explicit allowlist. Free text that a
buyer, agent, or business controls (exception messages, error details, request
text, notes, addresses) is never copied. Building the note is a side channel: no
failure here may change the error output it describes.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import sys
import sysconfig
import traceback
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
import typer

from .output import AgentOutput, emit, emit_error
from .session import (
    AvailabilityWindow,
    BookingRef,
    Business,
    LastError,
    PaymentRef,
    Session,
    SlotRef,
    load_session,
    save_session,
)

# Apps Script web app owned by support@booking-agent.dev (ingest/apps-script).
DEFAULT_SUPPORT_INGEST_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbxfFz5rR9eqMpEsxBtWL4sB8hoLs1ZmHlsC7wan-xB3rOAG1TowUow1UF_ZDEHegzRFNA/exec"
)
# Apps Script answers a POST with a redirect to its echo host, where the
# script's JSON reply is served. Only this exact host pair is ever followed.
_APPS_SCRIPT_HOST = "script.google.com"
_APPS_SCRIPT_ECHO_HOST = "script.googleusercontent.com"
SKILL_VERSION = "0.39.0"
BUNDLE_VERSION = 1
MAX_BUNDLE_BYTES = 64 * 1024
SEND_TIMEOUT_S = 10.0

SUPPORT_REPORT_COMMAND = "booking-agent flow support-report"
SEND_COMMAND = "booking-agent flow support-report send --user-confirmed"
DECLINE_COMMAND = "booking-agent flow support-report decline"
AWAITING_SUPPORT_REPORT_CONSENT = "support_report_consent"

CONSENT_MESSAGE = (
    "Something went wrong that I can't fix from here. May I send a note to the team "
    "that looks after the booking assistant? It describes what went wrong and the "
    "booking steps so far, and leaves out your name, phone, email, address, and "
    "payment details."
)
RETRY_CONSENT_MESSAGE = (
    "Last time I couldn't send the note to the team that looks after the booking "
    "assistant. Shall I try again? It still leaves out your name, phone, email, "
    "address, and payment details."
)
SENT_MESSAGE = "I've sent the note to the team. Thank you."
SEND_FAILED_MESSAGE = (
    "I couldn't send the note to the team just now. Nothing about your booking has "
    "changed."
)

# Codes the skill's recovery tables (Errors, the Step 6c-iii matrix, and cancel)
# already explain, plus codes its prose documents. Kept equal to the skill by
# test_support_report.py.
SKILL_TABLE_CODES = frozenset({
    "approval_not_surfaced_to_user", "approval_timeout", "booking_canceled_after_payment",
    "buyer_email_invalid", "buyer_not_from_link_cli", "calendar_gate_not_completed",
    "cancel_failed", "cancel_forbidden", "cancel_open_selection_conflict",
    "cancel_open_selection_required", "cancel_unauthorized", "checkout_already_in_progress",
    "checkout_prereq_not_completed", "checkout_total_unconfirmed", "complete_in_progress",
    "context_required", "disclosures_unconfirmed", "link_auth_expired",
    "link_auth_gate_not_completed", "link_auth_timeout", "link_cli_card_retrieve_failed",
    "mandatory_acceptance_pending", "missing_network_id_for_spt", "payment_already_captured",
    "payment_authentication_unsupported", "payment_captured_booking_unconfirmed",
    "payment_declined", "payment_failed", "payment_not_processed", "payment_timing_changed",
    "payment_timing_not_supported", "payment_unrecoverable", "profile_unreachable",
    "redirect_checkout_not_supported", "reschedule_at_business",
    "reschedule_booking_not_active", "reschedule_failed", "reschedule_fee_unsupported",
    "reschedule_not_actionable", "reschedule_not_allowed", "reschedule_price_changed",
    "reschedule_price_unknown", "reschedule_same_slot", "reschedule_unauthorized",
    "service_details_changed", "service_not_available_in_area",
    "shared_payment_token_not_issued", "slot_not_selected", "spend_request_not_approved",
    "spend_request_replacement_required", "timezone_confirmation_required",
    "ucp_complete_failed", "ucp_native_required", "webhook_wait_not_supported",
})
SKILL_PROSE_CODES = frozenset({
    "calendar_insufficient_scope", "calendar_oauth_not_configured", "calendar_oauth_timeout",
    "session_active", "session_not_found", "use_ucp_complete",
})
EXPECTED_CODES = SKILL_TABLE_CODES | SKILL_PROSE_CODES
# The business is still working; seeing these again is progress, not a failed step.
STILL_WORKING_CODES = frozenset({"complete_in_progress", "checkout_already_in_progress"})
POLL_COMMANDS = frozenset({"book poll", "pay poll"})
NEVER_OFFER_CODES = frozenset({
    "no_support_report",
    "support_report_not_confirmed",
    "support_report_send_failed",
    "support_report_unavailable",
})
# A terminal stop -> the commands its output forbids. Errors from those commands
# afterwards are the agent breaking a rule, not a product failure.
TERMINAL_STOP_FORBIDDEN: Dict[str, Tuple[str, ...]] = {
    "payment_unrecoverable": ("flow pay ucp-complete", "flow pay request"),
}
MAX_SEND_ATTEMPTS = 2
MAX_COMMAND_CHARS = 4000

# ---------------------------------------------------------------------------
# Leaf projectors: each returns a safe JSON value or None, and never raises on
# an unexpected type.
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")
_TOKEN_RE = re.compile(r"^[a-z0-9_.]{1,64}$")
_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?)?(?:Z|[+-]\d{2}:?\d{2})?$"
)
_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]{0,30}(?:/[A-Za-z0-9_+\-]{1,30}){0,3}$")
_DURATION_RE = re.compile(r"^P[0-9YMWDTHS.]{1,30}$")
_FLAG_RE = re.compile(r"^--[a-z][a-z0-9-]{0,40}$")
_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_QUALNAME_RE = re.compile(r"^[A-Za-z0-9_.<>]{1,200}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_PROJECT_PACKAGES = ("booking_agent_cli", "booking_agent_superagent_cli", "booking_agent")


def _id(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and _ID_RE.match(v) else None


def _token(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and _TOKEN_RE.match(v) else None


def _ts(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and _TS_RE.match(v) else None


def _tz(v: Any) -> Optional[str]:
    return v if isinstance(v, str) and _TZ_RE.match(v) else None


def _bool(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _label(v: Any) -> Optional[str]:
    """Public display names (business, service): single line, capped."""
    if not isinstance(v, str):
        return None
    text = " ".join(_CONTROL_RE.sub(" ", v).split())
    return text[:120] or None


def _url(v: Any) -> Optional[str]:
    if not isinstance(v, str) or len(v) > 2048:
        return None
    try:
        parts = urlsplit(v)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    except ValueError:
        return None
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))[:300]


def _duration(v: Any) -> Any:
    if isinstance(v, str):
        return v if _DURATION_RE.match(v) or _ID_RE.match(v) else None
    if isinstance(v, dict):
        out = {k: _duration(v.get(k)) for k in ("fixed", "min", "max") if k in v}
        return out or None
    return _num(v)


def _list(v: Any, fn: Callable[[Any], Any], limit: int = 50) -> Optional[list]:
    if not isinstance(v, list):
        return None
    out = [item for item in (fn(x) for x in v[:limit]) if item is not None]
    return out


_AMOUNT_KEYS: Dict[str, Callable[[Any], Any]] = {
    "amount": _num, "amount_minor": _num, "minor_units": _num, "value": _num,
    "decimal_places": _num, "currency": _id, "model": _id, "payment_timing": _id,
}


def _amountish(v: Any) -> Any:
    if isinstance(v, dict):
        out = {k: fn(v.get(k)) for k, fn in _AMOUNT_KEYS.items() if k in v}
        return out or None
    return _num(v)


def _terms(v: Any, depth: int = 0) -> Any:
    """Payment terms, schedules, disclosures: ids, kinds, amounts, currency, dates."""
    if depth > 3:
        return None
    if isinstance(v, list):
        return [t for t in (_terms(x, depth + 1) for x in v[:20]) if t is not None]
    if not isinstance(v, dict):
        return None
    out: Dict[str, Any] = {}
    for key, item in v.items():
        if key in ("id", "term_id", "kind", "type", "code", "timing", "model", "currency"):
            out[key] = _id(item)
        elif key in ("amount", "price", "due_now"):
            out[key] = _amountish(item)
        elif key in ("amount_minor", "minor_units", "due_now_minor", "percent", "percentage"):
            out[key] = _num(item)
        elif key in ("due", "due_at", "due_date"):
            out[key] = _ts(item)
        elif key in ("schedule", "schedules", "installments", "payments", "items"):
            out[key] = _terms(item, depth + 1)
    return out or None


def _quote(v: Any) -> Any:
    if not isinstance(v, dict):
        return None
    out: Dict[str, Any] = {}
    for key, item in v.items():
        if not isinstance(key, str) or not _KEY_RE.match(key):
            continue
        if key == "currency":
            out[key] = _id(item)
        elif _num(item) is not None or isinstance(item, bool):
            out[key] = item
    return out or None


def _messages(v: Any) -> Optional[list]:
    def one(m: Any) -> Any:
        if not isinstance(m, dict) or _token(m.get("code")) is None:
            return None
        return {"code": m.get("code"), "severity": _token(m.get("severity"))}

    return _list(v, one, 20)


def _capabilities(v: Any) -> Any:
    if not isinstance(v, dict):
        return None
    out = {}
    for ns, versions in list(v.items())[:30]:
        if _id(ns):
            out[ns] = _list(versions, _id, 10) or []
    return out


def _live_service(v: Any) -> Any:
    if not isinstance(v, dict):
        return None
    pricing = v.get("pricing") if isinstance(v.get("pricing"), dict) else None
    channels = v.get("channels")
    channel = v.get("channel")
    channel_types = _list(channels, lambda c: _id(c.get("type")) if isinstance(c, dict) else _id(c), 10)
    if channel_types is None and isinstance(channel, dict):
        channel_types = [t for t in [_id(channel.get("type"))] if t]
    return {
        "id": _id(v.get("id")),
        "service_name": _label(v.get("name")),
        "duration": _duration(v.get("duration")),
        "price": _amountish(pricing if pricing is not None else v.get("price")),
        "payment_timing": _id(v.get("payment_timing") or (pricing or {}).get("payment_timing")),
        "channel_types": channel_types,
    }


def _slot(v: Any) -> Any:
    if isinstance(v, SlotRef):
        get = lambda k: getattr(v, k, None)  # noqa: E731
    elif isinstance(v, dict):
        get = v.get
    else:
        return None
    out = {}
    for name, rule in SLOT_RULES.items():
        key, fn = rule
        out[key] = fn(get(name))
    return out


def _discover(v: Any) -> Optional[list]:
    def one(row: Any) -> Any:
        if not isinstance(row, dict):
            return None
        return {"service_id": _id(row.get("service_id")), "service_name": _label(row.get("service_name"))}

    return _list(v, one, 20)


def _timings(v: Any) -> Any:
    if not isinstance(v, dict):
        return None
    out = {}
    for step, item in list(v.items())[:50]:
        if not (isinstance(step, str) and _KEY_RE.match(step)):
            continue
        if isinstance(item, dict):
            out[step] = {k: x for k, x in item.items() if isinstance(k, str) and _KEY_RE.match(k) and _num(x) is not None}
        elif _num(item) is not None:
            out[step] = item
    return out


def _window(v: Any) -> Any:
    if not isinstance(v, AvailabilityWindow):
        return None
    return {"from": _ts(v.from_), "to": _ts(v.to)}


def _last_error(v: Any) -> Any:
    return {"code": _id(v.code)} if isinstance(v, LastError) else None


# ---------------------------------------------------------------------------
# Session projection. Every dataclass field maps to EXCLUDE or (out_key, fn);
# a field missing from its table fails the classification test.
# ---------------------------------------------------------------------------

EXCLUDE = None
FieldRule = Optional[Tuple[str, Callable[[Any], Any]]]

BUSINESS_RULES: Dict[str, FieldRule] = {
    "profile_url": ("profile_url", _url),
    "rest_endpoint": ("rest_endpoint", _url),
    "name": ("business_name", _label),
    "timezone": ("timezone", _tz),
}
BOOKING_RULES: Dict[str, FieldRule] = {
    "id": ("id", _id),
    "checkout_path": ("checkout_path", _id),
    "continue_url": EXCLUDE,
    "payment_context": EXCLUDE,
    "action_expires_at": ("action_expires_at", _ts),
    "endpoint": ("endpoint", _url),
    "credential": ("scoped_auth_present", lambda v: bool(v)),
    "credential_expires_at": ("scoped_auth_expires_at", _ts),
    "credential_jkt": EXCLUDE,
    "credential_scope": ("scoped_auth_scope", lambda v: _list(v, _id, 20)),
    "revision": ("revision", _id),
    "booking_url": EXCLUDE,
    "status": ("status", _id),
}
PAYMENT_RULES: Dict[str, FieldRule] = {
    "status": ("status", _id),
    "spend_request_id": ("spend_request_id", _id),
    "order_id": ("order_id", _id),
    "spend_approval_surfaced": ("spend_approval_surfaced", _bool),
    "spend_approval_user_surfaced": ("spend_approval_user_surfaced", _bool),
    "approved_at": ("approved_at", _ts),
    "spend_action_surfaced": ("spend_action_surfaced", _id),
    "payment_method_id": EXCLUDE,
    "declined_spend_request_id": ("declined_spend_request_id", _id),
    "declined_code": ("declined_code", _token),
    "declined_checkout_id": ("declined_checkout_id", _id),
    "captured_unconfirmed": ("captured_unconfirmed", _bool),
}
SLOT_RULES: Dict[str, FieldRule] = {
    "slot_id": ("slot_id", _id),
    "start": ("start", _ts),
    "end": ("end", _ts),
    "duration": ("duration", _duration),
    "price_amount": ("price_amount", _num),
    "alternate_slot_ids": ("alternate_slot_ids", lambda v: _list(v, _id, 10) or []),
}
WINDOW_RULES: Dict[str, FieldRule] = {"from_": ("from", _ts), "to": ("to", _ts)}
LAST_ERROR_RULES: Dict[str, FieldRule] = {"code": ("code", _id), "message": EXCLUDE}


def _nested(rules: Dict[str, FieldRule]) -> Callable[[Any], Any]:
    def project(obj: Any) -> Any:
        if obj is None:
            return None
        out = {}
        for name, rule in rules.items():
            if rule is None:
                continue
            key, fn = rule
            try:
                out[key] = fn(getattr(obj, name, None))
            except Exception:
                out[key] = None
        return out

    return project


SESSION_RULES: Dict[str, FieldRule] = {
    "id": ("id", _id),
    "correlation_id": EXCLUDE,  # top-level `correlation_id`
    "created_at": ("created_at", _ts),
    "user_request": EXCLUDE,
    "schema_version": ("schema_version", _num),
    "flow_phase": ("flow_phase", _id),
    "secrets_mode": EXCLUDE,  # `env.secrets_mode`
    "business": ("business", _nested(BUSINESS_RULES)),
    "service_timezone": ("service_timezone", _tz),
    "buyer_timezone": ("buyer_timezone", _tz),
    "buyer_timezone_source": ("buyer_timezone_source", _id),
    "buyer_timezone_error": ("buyer_timezone_error", _id),
    "timezone_difference_shown": ("timezone_difference_shown", _bool),
    "slots_presented_dual_zone": ("slots_presented_dual_zone", _bool),
    "calendar_gate_completed": ("calendar_gate_completed", _bool),
    "calendar_state": ("calendar_state", _id),
    "calendar_mode": ("calendar_mode", _id),
    "link_auth_gate_completed": ("link_auth_gate_completed", _bool),
    "availability_window": ("availability_window", _window),
    "slots_presented": ("slots_presented", lambda v: _list(v, _slot, 50)),
    "slot_id": ("slot_id", _id),
    "slot_user_confirmed": ("slot_user_confirmed", _bool),
    "service_id": ("service_id", _id),
    "selection_mismatch": EXCLUDE,
    "timezone_conflict_confirmed": ("timezone_conflict_confirmed", _bool),
    "live_service": ("live_service", _live_service),
    "payment_timing": ("payment_timing", _id),
    "payment_route": ("payment_route", _id),
    "selected_term_id": ("selected_term_id", _id),
    "payment_schedules": ("payment_schedules", _terms),
    "due_now_amount_minor": ("due_now_amount_minor", _num),
    "payment_disclosures": ("payment_disclosures", _terms),
    "disclosures_user_confirmed": ("disclosures_user_confirmed", _bool),
    "total_confirmed_amount_minor": ("total_confirmed_amount_minor", _num),
    "accepted_term": ("accepted_term", _terms),
    "complete_checkout_idempotency_key": EXCLUDE,
    "checkout_mode": ("checkout_mode", _id),
    "supported_checkout_modes": ("supported_checkout_modes", lambda v: _list(v, _id, 10)),
    "checkout_prereq_completed": ("checkout_prereq_completed", _bool),
    "ucp_checkout_rest_base": ("ucp_checkout_rest_base", _url),
    "ucp_checkout_session_id": ("ucp_checkout_session_id", _id),
    "ucp_checkout_amount_minor": ("ucp_checkout_amount_minor", _num),
    "ucp_merchant_quote": ("ucp_merchant_quote", _quote),
    "ucp_checkout_links": EXCLUDE,
    "ucp_checkout_policies": EXCLUDE,
    "ucp_checkout_messages": ("ucp_checkout_messages", _messages),
    "ucp_spt_network_id": ("ucp_spt_network_id", _id),
    "ucp_protocol_version": ("ucp_protocol_version", _id),
    "ucp_negotiated_capabilities": ("ucp_negotiated_capabilities", _capabilities),
    "ucp_capabilities_authoritative": ("ucp_capabilities_authoritative", _bool),
    "ucp_checkout_cancel_attempted": ("ucp_checkout_cancel_attempted", _bool),
    "ucp_checkout_cancel_succeeded": ("ucp_checkout_cancel_succeeded", _bool),
    "ucp_checkout_cancel_error": EXCLUDE,
    "buyer_source": ("buyer_source", _label),
    "buyer": EXCLUDE,
    "booking": ("booking", _nested(BOOKING_RULES)),
    "payment": ("payment", _nested(PAYMENT_RULES)),
    "last_error": ("last_error", _last_error),
    "activity_log": EXCLUDE,  # top-level `activity`
    "browser_channel": ("browser_channel", _id),
    "local_return_listener": ("local_return_listener", _bool),
    "return_listener_url": EXCLUDE,
    "discover_query": EXCLUDE,
    "discover_location_lat": EXCLUDE,
    "discover_location_lng": EXCLUDE,
    "discover_location_radius_km": ("discover_location_radius_km", _num),
    "discover_registry_url": ("discover_registry_url", _url),
    "discover_registry_request_body": EXCLUDE,
    "discover_services": ("discover_services", _discover),
    "flow_timings": ("flow_timings", _timings),
    "human_gate_timings": ("human_gate_timings", _timings),
    "skill_compliance_validated": ("skill_compliance_validated", _bool),
    "reschedule_original_slot_id": ("reschedule_original_slot_id", _id),
    "reschedule_slots": ("reschedule_slots", lambda v: _list(v, _slot, 50)),
    "reschedule_slot_id": ("reschedule_slot_id", _id),
    "reschedule_user_confirmed": ("reschedule_user_confirmed", _bool),
    "support_report_declined": EXCLUDE,
    "support_report_asked": EXCLUDE,
    "support_report_bytes": EXCLUDE,
    "support_report_recovery_command": EXCLUDE,
    "support_report_send_failures": EXCLUDE,
}

# Dataclass -> its rules table, for the classification test.
CLASSIFIED = {
    Session: SESSION_RULES,
    Business: BUSINESS_RULES,
    BookingRef: BOOKING_RULES,
    PaymentRef: PAYMENT_RULES,
    SlotRef: SLOT_RULES,
    AvailabilityWindow: WINDOW_RULES,
    LastError: LAST_ERROR_RULES,
}


def unclassified_fields() -> List[str]:
    missing = []
    for cls, rules in CLASSIFIED.items():
        for f in dataclass_fields(cls):
            if f.name not in rules:
                missing.append(f"{cls.__name__}.{f.name}")
    return missing


def project_session(session: Session) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, rule in SESSION_RULES.items():
        if rule is None:
            continue
        key, fn = rule
        try:
            out[key] = fn(getattr(session, name, None))
        except Exception:
            out[key] = None
    business = out.get("business")
    if isinstance(business, dict):
        business["ucp_checkout"] = getattr(session, "checkout_mode", None) == "ucp_native"
    return out


# ---------------------------------------------------------------------------
# Commands, frames, environment
# ---------------------------------------------------------------------------

def _root_command() -> Any:
    from .main import app as main_app

    return typer.main.get_command(main_app)


def _sanitize_one(tokens: List[str], root: Any) -> str:
    out = ["booking-agent"]
    rest = tokens[1:]
    group = root
    i = 0
    while i < len(rest):
        commands = getattr(group, "commands", None)
        sub = commands.get(rest[i]) if isinstance(commands, dict) else None
        if sub is None:
            break
        out.append(rest[i])
        group = sub
        i += 1
    for tok in rest[i:]:
        name = tok.split("=", 1)[0]
        if _FLAG_RE.match(name):
            out.append(name)
    return " ".join(out)


def sanitize_command(text: Optional[str]) -> Optional[str]:
    """Keep the subcommand path and flag names; drop every value."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        root = _root_command()
    except Exception:
        root = None
    parts = []
    for chunk in text.split("&&"):
        try:
            tokens = shlex.split(chunk)
        except ValueError:
            tokens = chunk.split()
        if tokens:
            parts.append(_sanitize_one(tokens, root))
    return " && ".join(parts)[:MAX_COMMAND_CHARS] or None


def _current_click_context() -> Any:
    # Typer >= 0.27 vendors click as typer._click with its own context stack.
    for module in ("typer._click.globals", "click.globals"):
        try:
            mod = __import__(module, fromlist=["get_current_context"])
            ctx = mod.get_current_context(silent=True)
        except Exception:
            continue
        if ctx is not None:
            return ctx
    return None


def current_command_path() -> Optional[str]:
    try:
        ctx = _current_click_context()
        names = []
        while ctx is not None and ctx.parent is not None:
            names.append(ctx.info_name or "")
            ctx = ctx.parent
        return " ".join(reversed(names)) or None
    except Exception:
        return None


def _strip_flow(path: str) -> str:
    path = path.strip()
    return path[len("flow "):] if path.startswith("flow ") else path


def _qualname(exc: BaseException) -> Optional[str]:
    cls = type(exc)
    name = f"{cls.__module__}.{cls.__qualname__}"
    return name if _QUALNAME_RE.match(name) else None


def _frame_file(filename: str) -> Optional[str]:
    parts = PurePath(filename).parts
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            idx = len(parts) - 1 - parts[::-1].index(marker)
            rest = parts[idx + 1:]
            return "/".join(rest) if rest else None
    for idx in range(len(parts) - 1, -1, -1):
        if parts[idx] in _PROJECT_PACKAGES:
            return "/".join(parts[idx:])
    stdlib = sysconfig.get_paths().get("stdlib")
    if stdlib and filename.startswith(stdlib + os.sep):
        return "stdlib/" + PurePath(os.path.relpath(filename, stdlib)).as_posix()
    return None


def _exception(exc: Optional[BaseException]) -> Optional[Dict[str, Any]]:
    if exc is None:
        return None
    chain: List[Optional[str]] = []
    seen = {id(exc)}
    cur = exc.__cause__ or exc.__context__
    while cur is not None and len(chain) < 5 and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(_qualname(cur))
        cur = cur.__cause__ or cur.__context__
    frames = []
    try:
        for fs in traceback.extract_tb(exc.__traceback__)[-40:]:
            file = _frame_file(fs.filename)
            function = fs.name if isinstance(fs.name, str) and _QUALNAME_RE.match(fs.name) else None
            code = (fs.line or "").strip()[:200] if file else None
            frames.append({
                "file": file or "<external>",
                "line": fs.lineno,
                "function": function,
                "code": code or None,
            })
    except Exception:
        frames = []
    return {"type": _qualname(exc), "chain": chain, "frames": frames}


def _cli_build() -> Optional[str]:
    from importlib.metadata import distribution

    raw = distribution("booking-agent-cli").read_text("direct_url.json")
    if not raw:
        return None
    commit = (json.loads(raw).get("vcs_info") or {}).get("commit_id")
    return commit if isinstance(commit, str) and re.match(r"^[0-9a-f]{7,64}$", commit) else None


def _safe_call(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:
        return None


def _env(session: Session) -> Dict[str, Any]:
    def cli_version() -> Optional[str]:
        from . import __version__

        return _id(__version__)

    def core_version() -> Optional[str]:
        from booking_agent import __version__

        return _id(__version__)

    return {
        "skill_version": SKILL_VERSION,
        "cli_version": _safe_call(cli_version),
        "cli_build": _safe_call(_cli_build),
        "core_version": _safe_call(core_version),
        "python": _safe_call(lambda: _id(platform.python_version())),
        "os": _safe_call(lambda: _label(f"{platform.system()} {platform.release()}")),
        "machine": _safe_call(lambda: _id(platform.machine())),
        "secrets_mode": _id(getattr(session, "secrets_mode", None)),
    }


def _occurrences(session: Session, code: str) -> int:
    return 1 + sum(
        1 for e in (session.activity_log or []) if e.kind == "error" and e.code == code
    )


def _activity(session: Session) -> List[Dict[str, Any]]:
    out = []
    for entry in (session.activity_log or [])[-50:]:
        kind = getattr(entry, "kind", None)
        out.append({
            "at": _ts(getattr(entry, "at", None)),
            "kind": kind if kind in ("command", "error") else None,
            "command": sanitize_command(getattr(entry, "text", None)) if kind == "command" else None,
            "code": _id(getattr(entry, "code", None)),
        })
    return out


def _http() -> Optional[Dict[str, Any]]:
    from booking_agent.http_trace import last_http_failure

    return last_http_failure()


# ---------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minimal_bundle(code: str, build_errors: List[Dict[str, Any]]) -> str:
    try:
        created = _now()
    except Exception:
        created = "1970-01-01T00:00:00+00:00"
    try:
        from . import __version__ as cli_version
    except Exception:
        cli_version = None
    return json.dumps({
        "bundle_version": BUNDLE_VERSION,
        "created_at": created,
        "env": {"cli_version": _id(cli_version)},
        "error": {"code": _id(code)},
        "build_errors": build_errors[:10],
    }, sort_keys=True, separators=(",", ":"))


def _serialize(bundle: Dict[str, Any]) -> str:
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str)


def _cap(bundle: Dict[str, Any]) -> str:
    text = _serialize(bundle)
    while len(text.encode("utf-8")) > MAX_BUNDLE_BYTES:
        activity = bundle.get("activity")
        exc = bundle.get("exception") or {}
        session = bundle.get("session") or {}
        if activity:
            bundle["activity"] = activity[len(activity) // 2 + 1:] if len(activity) > 1 else []
        elif exc.get("frames"):
            frames = exc["frames"]
            exc["frames"] = frames[len(frames) // 2 + 1:] if len(frames) > 1 else []
        elif session.get("discover_services"):
            session["discover_services"] = []
        else:
            raise ValueError("bundle too large")
        text = _serialize(bundle)
    return text


def build_bundle(
    session: Session,
    *,
    code: str,
    phase: Optional[str],
    next_command: Optional[str],
    reason: str,
    exc: Optional[BaseException] = None,
) -> str:
    """Serialized report; never raises."""
    build_errors: List[Dict[str, Any]] = []

    def section(name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            build_errors.append({"section": name, "type": type(e).__name__[:64]})
            return None

    try:
        from booking_agent.http_trace import _redact_json_value

        bundle: Dict[str, Any] = {
            "bundle_version": BUNDLE_VERSION,
            "created_at": _now(),
            "correlation_id": section("correlation_id", lambda: _id(session.correlation_id)),
            "env": section("env", lambda: _env(session)),
            "error": section("error", lambda: {
                "code": _id(code),
                "phase": _id(phase),
                "occurrences": _occurrences(session, code),
                "offer_reason": reason,
                "next_command_before_offer": sanitize_command(next_command),
            }),
            "exception": section("exception", lambda: _exception(exc)),
            "http": section("http", _http),
            "session": section("session", lambda: project_session(session)),
            "activity": section("activity", lambda: _activity(session)),
        }
        bundle["build_errors"] = build_errors
        if bundle["env"] is None or bundle["error"] is None:
            return _minimal_bundle(code, build_errors)
        return _cap(_redact_json_value(bundle))
    except Exception as e:  # noqa: BLE001
        return _minimal_bundle(code, build_errors + [{"section": "serialize", "type": type(e).__name__[:64]}])


# ---------------------------------------------------------------------------
# Offer policy
# ---------------------------------------------------------------------------

def _forbidden_after_terminal_stop(session: Session, command_path: Optional[str]) -> bool:
    if session.flow_phase != "FAILED" or not command_path:
        return False
    last_stop = next(
        (e.code for e in reversed(session.activity_log or [])
         if e.kind == "error" and e.code in TERMINAL_STOP_FORBIDDEN),
        None,
    )
    if last_stop is None:
        return False
    path = _strip_flow(command_path)
    return any(_strip_flow(cmd) == path for cmd in TERMINAL_STOP_FORBIDDEN[last_stop])


def _repeated(session: Session, code: str) -> bool:
    log = session.activity_log or []
    last = max((i for i, e in enumerate(log) if e.kind == "error" and e.code == code), default=None)
    if last is None:
        return False
    return any(e.kind == "command" for e in log[last + 1:])


def _command_path_of(next_command: Optional[str]) -> Optional[str]:
    if not isinstance(next_command, str):
        return None
    words = []
    for word in next_command.split("&&")[0].split():
        if word.startswith("-") or word.startswith("<"):
            break
        words.append(word)
    if words and words[0] == "booking-agent":
        words = words[1:]
    return " ".join(words) or None


def _is_wait_loop(next_command: Optional[str], command_path: Optional[str]) -> bool:
    """A polling command that tells the agent to poll again is still waiting."""
    path = _command_path_of(next_command)
    if not path or not command_path:
        return False
    path = _strip_flow(path)
    return path in POLL_COMMANDS and path == _strip_flow(command_path)


def offer_reason(
    session: Session,
    code: str,
    next_command: Optional[str],
    command_path: Optional[str] = None,
) -> Optional[str]:
    if code in NEVER_OFFER_CODES:
        return None
    if session.support_report_declined or session.support_report_bytes:
        return None
    if _forbidden_after_terminal_stop(session, command_path):
        return None
    if code == "internal_error":
        return "internal_error"
    if code == "payment_captured_booking_unconfirmed" and not next_command:
        return "captured_no_next"
    # An undocumented code that still hands back a recovery command is a planned
    # CLI outcome; only a dead end counts as unknown.
    if code not in EXPECTED_CODES and not next_command:
        return "unknown_code"
    if code in STILL_WORKING_CODES or _is_wait_loop(next_command, command_path):
        return None
    if _repeated(session, code):
        return "repeated"
    return None


def maybe_offer(
    session: Session,
    code: str,
    next_command: Optional[str],
    phase: Optional[str],
    exc: Optional[BaseException] = None,
) -> bool:
    """Freeze a report on ``session`` when the policy offers one. Caller saves."""
    reason = offer_reason(session, code, next_command, current_command_path())
    if reason is None:
        return False
    payload = build_bundle(
        session, code=code, phase=phase, next_command=next_command, reason=reason, exc=exc,
    )
    session.support_report_bytes = payload
    session.support_report_recovery_command = next_command
    session.support_report_asked = False
    session.support_report_send_failures = 0
    return True


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_CORRUPT = object()


def _snapshot(session: Session) -> Any:
    raw = session.support_report_bytes
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return _CORRUPT
    return parsed if isinstance(parsed, dict) else _CORRUPT


def _restore(session: Session, *, declined: bool = False) -> Optional[str]:
    from .engine import recommend_next

    recovery = session.support_report_recovery_command
    session.support_report_bytes = None
    session.support_report_asked = False
    session.support_report_recovery_command = None
    session.support_report_send_failures = 0
    if declined:
        session.support_report_declined = True
    save_session(session, write_support_report=True)
    return recovery or recommend_next(session)


def _emit_unavailable(session: Session) -> "typer.Exit":
    next_cmd = _restore(session)
    emit_error(
        session.flow_phase,
        code="support_report_unavailable",
        message=(
            "The saved note could not be read, so there is nothing to send. Do not ask "
            "the user about it. Continue with next_command."
        ),
        next_command=next_cmd,
    )
    return typer.Exit(code=1)


def consent_output(session: Session, report: Dict[str, Any]) -> AgentOutput:
    """The consent gate shown by `support-report` and by `flow status`."""
    first_ask = not session.support_report_asked
    if first_ask:
        session.support_report_asked = True
        save_session(session, write_support_report=True)
        message = RETRY_CONSENT_MESSAGE if session.support_report_send_failures else CONSENT_MESSAGE
        prompt = (
            "STOP. Show user_message to the user exactly as written and wait for their "
            "answer. If they agree, run next_command. If they say no, run "
            f"`{DECLINE_COMMAND}`. Do not write, edit, or send the note yourself."
        )
    else:
        message = None
        prompt = (
            "You already asked the user whether to send the note. Do not ask again. Wait "
            "for their answer: if they agree, run next_command; if they say no, run "
            f"`{DECLINE_COMMAND}`."
        )
    return AgentOutput(
        phase=session.flow_phase,
        awaiting_user=AWAITING_SUPPORT_REPORT_CONSENT,
        user_message=message,
        prompt=prompt,
        next_command=SEND_COMMAND,
        forbidden=[
            "Sending the note without the user's yes",
            "Writing, editing, or emailing the note yourself",
        ],
        data={"report": report, "decline_command": DECLINE_COMMAND},
    )


def status_gate(session: Session) -> Optional[AgentOutput]:
    """Consent gate for `flow status` while a note waits for the user's answer."""
    try:
        report = _snapshot(session)
        if report is None:
            return None
        if report is _CORRUPT:
            _restore(session)
            return None
        if session.support_report_send_failures:
            return None
        return consent_output(session, report)
    except Exception:
        return None


def _load_or_exit() -> Session:
    session = load_session()
    if session is None:
        emit_error(
            "INIT",
            code="no_support_report",
            message="No active booking, so there is no note to send.",
        )
        raise typer.Exit(code=1)
    return session


def _no_pending(session: Session) -> None:
    from .engine import recommend_next

    emit(AgentOutput(
        phase=session.flow_phase,
        prompt="There is no note waiting to be sent. Continue with next_command.",
        next_command=recommend_next(session),
        data={"sent": False, "pending": False},
    ))


def _apps_script_echo_url(url: str, response: httpx.Response) -> Optional[str]:
    if response.status_code not in (302, 303):
        return None
    location = response.headers.get("location") or ""
    try:
        origin, target = urlsplit(url), urlsplit(location)
    except ValueError:
        return None
    if (
        origin.scheme == "https" and origin.hostname == _APPS_SCRIPT_HOST
        and target.scheme == "https" and target.hostname == _APPS_SCRIPT_ECHO_HOST
    ):
        return location
    return None


def _post_report(url: str, body: bytes, *, transport: Optional[httpx.BaseTransport] = None) -> int:
    """POST the note; returns an HTTP-like status where 2xx means delivered."""
    with httpx.Client(timeout=SEND_TIMEOUT_S, follow_redirects=False, transport=transport) as client:
        response = client.post(url, content=body, headers={"content-type": "application/json"})
        echo = _apps_script_echo_url(url, response)
        if echo is None:
            return response.status_code
        reply = client.get(echo)
        if reply.status_code != 200:
            return reply.status_code
        try:
            accepted = reply.json().get("accepted") is True
        except (ValueError, AttributeError):
            accepted = False
        return 200 if accepted else 422


support_app = typer.Typer(
    help="Consented note to the booking-assistant team after an unrecoverable failure.",
    invoke_without_command=True,
)


@support_app.callback()
def support_report(ctx: typer.Context) -> None:
    """Show the saved note and ask the user whether to send it."""
    if ctx.invoked_subcommand:
        return
    session = _load_or_exit()
    report = _snapshot(session)
    if report is None:
        _no_pending(session)
        return
    if report is _CORRUPT:
        raise _emit_unavailable(session)
    emit(consent_output(session, report))


@support_app.command("send")
def support_report_send(
    user_confirmed: bool = typer.Option(
        False, "--user-confirmed", help="The user agreed to send exactly this note.",
    ),
) -> None:
    """Send the saved note, unchanged, after the user agreed."""
    session = _load_or_exit()
    if not user_confirmed:
        emit_error(
            session.flow_phase,
            code="support_report_not_confirmed",
            message=(
                "Pass --user-confirmed only after the user said yes to sending the note. "
                f"Ask them with `{SUPPORT_REPORT_COMMAND}`."
            ),
            next_command=SUPPORT_REPORT_COMMAND,
        )
        raise typer.Exit(code=1)
    report = _snapshot(session)
    if report is None:
        _no_pending(session)
        return
    if report is _CORRUPT:
        raise _emit_unavailable(session)
    body = session.support_report_bytes.encode("utf-8")
    try:
        status = _post_report(DEFAULT_SUPPORT_INGEST_URL, body)
        failure = None if 200 <= status < 300 else f"http_{status}"
    except Exception as e:  # noqa: BLE001
        failure = type(e).__name__
    if failure is None:
        next_cmd = _restore(session)
        emit(AgentOutput(
            phase=session.flow_phase,
            user_message=SENT_MESSAGE,
            prompt="The note was sent. Show user_message, then continue with next_command.",
            next_command=next_cmd,
            data={"sent": True},
        ))
        return
    session.support_report_send_failures += 1
    if session.support_report_send_failures >= MAX_SEND_ATTEMPTS:
        next_cmd = _restore(session)
    else:
        session.support_report_asked = False
        save_session(session, write_support_report=True)
        next_cmd = session.support_report_recovery_command
        if not next_cmd:
            from .engine import recommend_next

            next_cmd = recommend_next(session)
    emit_error(
        session.flow_phase,
        code="support_report_send_failed",
        message=(
            f"The note could not be delivered ({failure}). Show user_message, then "
            "continue with next_command. Do not try to send it again unless the user asks."
        ),
        user_message=SEND_FAILED_MESSAGE,
        next_command=next_cmd,
    )
    raise typer.Exit(code=1)


@support_app.command("decline")
def support_report_decline() -> None:
    """The user said no: drop the note and never offer again in this booking."""
    session = _load_or_exit()
    next_cmd = _restore(session, declined=True)
    emit(AgentOutput(
        phase=session.flow_phase,
        prompt=(
            "The user does not want to send the note. Do not ask again. Continue with "
            "next_command."
        ),
        next_command=next_cmd,
        data={"sent": False, "declined": True},
    ))
