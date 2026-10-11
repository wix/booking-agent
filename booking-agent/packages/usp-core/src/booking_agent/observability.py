"""Flow correlation id propagation and machine-countable money-path events."""
from __future__ import annotations

import json
import sys
import uuid
from contextvars import ContextVar, Token
from typing import Any, Callable, Dict, Optional

import httpx

# Wire name for the flow join key. Optional on the USP HTTP binding (Track S of
# spec issue #160): send when bound, never require it on responses, never fail a
# call because a peer omitted or ignored it.
HEADER = "USP-Correlation-Id"

_FORBIDDEN_FIELD_KEYS = frozenset({
    "token",
    "credential",
    "shared_payment_token",
    "client_secret",
    "access_token",
    "refresh_token",
    "email",
    "phone",
    "phone_number",
    "first_name",
    "last_name",
    "name",
    "address",
    "authorization",
    "dpop",
    "spt",
    "pan",
})

_correlation_id: ContextVar[Optional[str]] = ContextVar("booking_agent_correlation_id", default=None)
_event_sink: Callable[[Dict[str, Any]], None] = lambda record: sys.stderr.write(
    json.dumps(record, default=str) + "\n"
)


def set_event_sink(sink: Callable[[Dict[str, Any]], None]) -> None:
    """Replace the default stderr JSON event sink (tests)."""
    global _event_sink
    _event_sink = sink


def reset_event_sink() -> None:
    """Restore the default stderr event sink."""
    global _event_sink
    _event_sink = lambda record: sys.stderr.write(json.dumps(record, default=str) + "\n")


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def bind(correlation_id: str) -> Token:
    return _correlation_id.set(correlation_id)


def current() -> Optional[str]:
    return _correlation_id.get()


def reset(token: Optional[Token] = None) -> None:
    if token is not None:
        _correlation_id.reset(token)
    else:
        _correlation_id.set(None)


def _sanitize_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in fields.items():
        key_l = str(key).lower()
        if key_l in _FORBIDDEN_FIELD_KEYS or "credential" in key_l or key_l.endswith("_token"):
            continue
        if isinstance(value, str) and ("?" in value and ("http://" in value or "https://" in value)):
            continue
        out[key] = value
    return out


def emit_event(event: str, **fields: Any) -> None:
    """Emit one machine-countable JSON event line."""
    record: Dict[str, Any] = {"event": event}
    cid = current()
    if cid:
        record["correlation_id"] = cid
    record.update(_sanitize_fields(fields))
    _event_sink(record)


def registry_error_class(response: Optional[httpx.Response]) -> str:
    """Map registry HTTP failures to #171-style classes."""
    if response is None:
        return "upstream_failure"
    status = response.status_code
    if status in {401, 403}:
        return "auth_rejection"
    if status in {400, 422}:
        return "malformed_request"
    if status >= 500:
        return "upstream_failure"
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        err_type = str(body.get("type") or "").lower()
        if "auth" in err_type or "unauthorized" in err_type:
            return "auth_rejection"
        if "malformed" in err_type or "invalid" in err_type:
            return "malformed_request"
    if status >= 400:
        return "unknown"
    return "unknown"


def payment_decline_reason_class(status: int, body: str = "") -> str:
    """Classify UCP/processor declines for ``payment_declined`` events."""
    lowered = (body or "").lower()
    if status in {402, 409} or "declin" in lowered:
        return "declined"
    if status >= 500 or "processor" in lowered:
        return "processor_error"
    return "unknown"


def money_path_fields(
    *,
    spend_request_id: Optional[str] = None,
    booking_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Bounded join keys for money-path events (log fields, not metric labels)."""
    fields: Dict[str, Any] = {}
    if spend_request_id:
        fields["spend_request_id"] = spend_request_id
    if booking_id:
        fields["booking_id"] = booking_id
    return fields
