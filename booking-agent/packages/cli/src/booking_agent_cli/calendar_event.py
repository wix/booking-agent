"""Create Google Calendar events after booking confirmation."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx

from booking_agent.calendar.scope import CALENDAR_MODE_EVENTS
from booking_agent.http_deadlines import coerce_timeout
from booking_agent_cli.session import Session

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
SIDECAR_NAME = ".calendar-events.json"


def _token_cache_dir() -> str:
    from booking_agent.settings import google_calendar_token_dir

    return str(google_calendar_token_dir())


def _user_cache_path(user_key: str) -> str:
    import hashlib

    os.makedirs(_token_cache_dir(), exist_ok=True)
    safe_name = hashlib.sha256(user_key.encode()).hexdigest()[:16]
    return os.path.join(_token_cache_dir(), f"{safe_name}.json")


def load_cached_token(user_key: str) -> Optional[Dict[str, Any]]:
    path = _user_cache_path(user_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def write_token_cache(user_key: str, access_token: str, expires_in: int = 3600) -> str:
    path = _user_cache_path(user_key)
    entry = {
        "access_token": access_token,
        "refresh_token": None,
        "expires_at": __import__("time").time() + expires_in,
        "user_key": user_key,
    }
    with open(path, "w") as f:
        json.dump(entry, f, indent=2)
    os.chmod(path, 0o600)
    return path


def _sidecar_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".booking-agent", SIDECAR_NAME)


def _load_sidecar() -> Dict[str, Any]:
    path = _sidecar_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_sidecar(data: Dict[str, Any]) -> None:
    path = _sidecar_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(path, 0o600)


def existing_event_id(session_id: str) -> Optional[str]:
    sidecar = _load_sidecar()
    entry = sidecar.get(session_id)
    if isinstance(entry, dict):
        return entry.get("event_id")
    return None


def record_event_id(session_id: str, event_id: str, html_link: Optional[str] = None) -> None:
    sidecar = _load_sidecar()
    entry: Dict[str, Any] = {"event_id": event_id}
    if html_link:
        entry["html_link"] = html_link
    sidecar[session_id] = entry
    _save_sidecar(sidecar)


_WELL_KNOWN_PROFILE_SUFFIXES = ("/.well-known/ucp", "/.well-known/usp")


def public_business_url(profile_url: str) -> str:
    """The site a person can open, not the /.well-known profile document."""
    raw = profile_url.strip()
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    for suffix in _WELL_KNOWN_PROFILE_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            return urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return raw


def selected_slot(session: Session) -> Optional[Dict[str, str]]:
    if not session.slot_id:
        return None
    for slot in session.slots_presented:
        if slot.slot_id == session.slot_id:
            return {
                "slot_id": slot.slot_id,
                "start": slot.start,
                "end": slot.end,
            }
    return None


def _service_display_name(session: Session) -> Optional[str]:
    """Human service name for the event title; the service id is never shown."""
    live = session.live_service if isinstance(session.live_service, dict) else {}
    for key in ("name", "title"):
        value = str(live.get(key) or "").strip()
        if value:
            return value
    for row in session.discover_services or []:
        if row.get("service_id") == session.service_id:
            value = str(row.get("service_name") or "").strip()
            if value:
                return value
    return None


def _live_service(session: Session) -> Dict[str, Any]:
    live = session.live_service
    return live if isinstance(live, dict) else {}


def _live_pricing(session: Session) -> Dict[str, Any]:
    pricing = _live_service(session).get("pricing")
    return pricing if isinstance(pricing, dict) else {}


def _payment_route(session: Session) -> Optional[str]:
    """The booked route, or the live catalog's route when the session never stored one."""
    if session.payment_route:
        return session.payment_route
    policies = _live_service(session).get("policies")
    policies = policies if isinstance(policies, dict) else {}
    pricing = _live_pricing(session)
    if policies.get("requires_payment") is False or pricing.get("model") == "free":
        timing = policies.get("payment_timing")
        if timing in (None, ""):
            return "free"
        return None
    timing = policies.get("payment_timing")
    if timing in ("at_booking", "at_service", "deposit_required"):
        return timing
    return None


def _currency(session: Session) -> Optional[str]:
    quote = session.ucp_merchant_quote if isinstance(session.ucp_merchant_quote, dict) else {}
    if quote.get("currency"):
        return str(quote["currency"])
    pricing = _live_pricing(session)
    if pricing.get("currency"):
        return str(pricing["currency"])
    context = session.booking.payment_context
    if isinstance(context, dict) and context.get("currency"):
        return str(context["currency"])
    for row in session.discover_services or []:
        if row.get("service_id") == session.service_id and row.get("currency"):
            return str(row["currency"])
    return None


def _minor(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict) and value.get("amount") is not None:
        return _minor(value.get("amount"))
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _schedule_amount(schedule: Dict[str, Any]) -> Optional[int]:
    return _minor(schedule.get("amount"))


def _total_minor(session: Session) -> Optional[int]:
    if session.ucp_checkout_amount_minor is not None:
        return int(session.ucp_checkout_amount_minor)
    quote = session.ucp_merchant_quote if isinstance(session.ucp_merchant_quote, dict) else {}
    quoted = _minor(quote.get("total_minor_units"))
    if quoted is not None:
        return quoted
    pricing = _live_pricing(session)
    for key in ("amount_minor_units", "amount"):
        amount = _minor(pricing.get(key))
        if amount is not None:
            return amount
    if session.slot_id:
        for slot in session.slots_presented:
            if slot.slot_id == session.slot_id and slot.price_amount is not None:
                return int(slot.price_amount)
    return None


def _money(amount: Optional[int], currency: Optional[str]) -> Optional[str]:
    if amount is None or not currency:
        return None
    from booking_agent_cli.catalog import _format_price_display

    return _format_price_display(int(amount), currency)


def _deposit_and_balance(
    session: Session, total: Optional[int],
) -> Tuple[Optional[int], Optional[int]]:
    deposit = _minor(session.due_now_amount_minor)
    later: list[int] = []
    for schedule in session.payment_schedules or []:
        if not isinstance(schedule, dict):
            continue
        amount = _schedule_amount(schedule)
        if amount is None:
            continue
        if schedule.get("type") == "immediate":
            if deposit is None:
                deposit = amount
        else:
            later.append(amount)
    balance = sum(later) if later else None
    if balance is None and deposit is not None and total is not None and total > deposit:
        balance = total - deposit
    return deposit, balance


def _payment_captured(session: Session) -> bool:
    """Money for this route was collected. Pay-at-service confirmations are not this."""
    status = (session.payment.status or "").lower()
    if status == "failed":
        return False
    if status == "paid" or session.payment.order_id:
        return True
    return session.flow_phase == "CONFIRMED"


def _payment_line(session: Session) -> Optional[str]:
    """One sentence for free, pay at the visit, deposit plus remainder, or paid in full."""
    route = _payment_route(session)
    currency = _currency(session)
    total = _total_minor(session)
    total_display = _money(total, currency)
    if route == "free":
        return "Payment: This service is free."
    if route == "at_service":
        if total_display:
            return f"Payment: Pay {total_display} when you receive the service."
        return "Payment: This needs to be paid when you receive the service."
    if route == "deposit_required":
        deposit, balance = _deposit_and_balance(session, total)
        deposit_display = _money(deposit, currency)
        balance_display = _money(balance, currency)
        if _payment_captured(session):
            if deposit_display and balance_display:
                return (
                    f"Payment: You paid a deposit of {deposit_display}. "
                    f"{balance_display} is still due when you receive the service."
                )
            if deposit_display:
                return (
                    f"Payment: You paid a deposit of {deposit_display}. "
                    "The remaining balance is still due when you receive the service."
                )
            if balance_display:
                return (
                    "Payment: You paid a deposit. "
                    f"{balance_display} is still due when you receive the service."
                )
            return (
                "Payment: You paid a deposit. The remaining balance is still due "
                "when you receive the service."
            )
        if deposit_display and balance_display:
            return (
                f"Payment: A deposit of {deposit_display} is due now, and "
                f"{balance_display} is due when you receive the service."
            )
        if balance_display:
            return (
                "Payment: A deposit is due now, and "
                f"{balance_display} is due when you receive the service."
            )
        return (
            "Payment: A deposit is due now, and the remaining balance is due "
            "when you receive the service."
        )
    if route == "at_booking":
        if _payment_captured(session):
            if total_display:
                return f"Payment: You already paid {total_display} for this service in full."
            return "Payment: You already paid for this service in full."
        if total_display:
            return f"Payment: The full price of {total_display} is due for this booking."
        return "Payment: The full price is due for this booking."
    return None


def _where_line(session: Session) -> Optional[str]:
    from .booking_summary import where_text

    text = where_text(session)
    return f"Where: {text}." if text else None


def _http_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip()
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return None


def _link_label(link: Dict[str, Any]) -> str:
    raw = str(link.get("type") or link.get("rel") or "link").strip()
    words = raw.replace("_", " ").replace("-", " ").strip()
    if not words:
        return "Link"
    return words[:1].upper() + words[1:]


def _fallback_links(session: Session) -> list[tuple[str, str]]:
    """Links the agent can already hand the buyer when no manage page was saved."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, url: Optional[str]) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        found.append((label, url))

    if session.business.profile_url:
        add("Business website", _http_url(public_business_url(session.business.profile_url)))
    for link in session.ucp_checkout_links or []:
        if not isinstance(link, dict):
            continue
        add(_link_label(link), _http_url(link.get("url") or link.get("href")))
    return found


def _change_instructions(session: Session) -> list[str]:
    """How to cancel or reschedule, using the manage page or the saved fallbacks."""
    manage = _http_url(session.booking.booking_url)
    if manage:
        return [
            "To cancel or reschedule this booking, open the manage booking page:",
            manage,
        ]
    fallbacks = _fallback_links(session)
    if not fallbacks:
        return [
            "To cancel or reschedule this booking, contact the business directly.",
        ]
    lead = "To cancel or reschedule this booking, check the links in the confirmation email"
    if len(fallbacks) == 1:
        label, url = fallbacks[0]
        return [f"{lead} or via the {label.lower()} link: {url}"]
    lines = [f"{lead} or via these links:"]
    lines.extend(f"{label}: {url}" for label, url in fallbacks)
    return lines


def _event_description(session: Session) -> str:
    """Plain-text summary of the booking for the calendar event body."""
    details: list[str] = []
    request = (session.user_request or "").strip()
    if request:
        details.append(f"Booking request: {request}")
    service_name = _service_display_name(session)
    if service_name:
        details.append(f"Service: {service_name}")
    buyer_name = (session.buyer.name or "").strip()
    if buyer_name:
        details.append(f"Booked for: {buyer_name}")
    where = _where_line(session)
    if where:
        details.append(where)
    note = (session.booking_notes or "").strip()
    if note:
        details.append(f"Your note to the business: {note}")
    payment = _payment_line(session)
    if payment:
        details.append(payment)
    if session.booking.id:
        details.append(f"Booking ID: {session.booking.id}")
    order_id = (session.payment.order_id or "").strip()
    if order_id:
        details.append(f"Order reference: {order_id}")
    if session.business.profile_url:
        details.append(f"Business: {public_business_url(session.business.profile_url)}")

    blocks = ["\n".join(details)] if details else []
    blocks.append("\n".join(_change_instructions(session)))
    return "\n\n".join(blocks)


def build_event_body(session: Session) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    slot = selected_slot(session)
    if not slot:
        return None, "No selected slot in session; cannot create calendar event."

    business_name = (session.business.name or "Appointment").strip()
    service_name = _service_display_name(session)
    summary = f"{business_name} - {service_name}" if service_name else business_name

    from .booking_summary import event_location
    from .timezone_context import valid_iana_timezone

    start: Dict[str, Any] = {"dateTime": slot["start"]}
    end: Dict[str, Any] = {"dateTime": slot["end"]}
    zone = valid_iana_timezone(session.business.timezone)
    if zone:
        start["timeZone"] = zone
        end["timeZone"] = zone
    body: Dict[str, Any] = {
        "summary": summary,
        "description": _event_description(session),
        "start": start,
        "end": end,
    }
    location = event_location(session)
    if location:
        body["location"] = location
    return body, None


def _refresh_host_token(session: Session) -> None:
    from .calendar_connection import refresh_host_token

    refresh_host_token(session, CALENDAR_MODE_EVENTS)


def create_calendar_event(session: Session, user_key: str = "cli") -> Dict[str, Any]:
    _refresh_host_token(session)
    cached = load_cached_token(user_key)
    if not cached or not cached.get("access_token"):
        return {
            "ok": False,
            "code": "calendar_not_connected",
            "message": "No calendar token in cache. Run calendar connect first.",
        }

    existing = existing_event_id(session.id)
    if existing:
        sidecar = _load_sidecar().get(session.id, {})
        html_link = sidecar.get("html_link") if isinstance(sidecar, dict) else None
        return {"ok": True, "event_id": existing, "html_link": html_link, "idempotent": True}

    event_body, err = build_event_body(session)
    if err or not event_body:
        return {"ok": False, "code": "calendar_event_invalid", "message": err or "invalid event"}

    access_token = cached["access_token"]
    with httpx.Client(timeout=coerce_timeout(20.0)) as client:
        resp = client.post(
            EVENTS_URL,
            json=event_body,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code == 403:
        return {
            "ok": False,
            "code": "calendar_write_failed",
            "message": resp.text,
        }
    if resp.status_code >= 400:
        return {
            "ok": False,
            "code": "calendar_write_failed",
            "message": f"Google Calendar API error {resp.status_code}: {resp.text}",
        }

    data = resp.json()
    event_id = data.get("id")
    html_link = data.get("htmlLink")
    if event_id:
        record_event_id(session.id, event_id, html_link)
    return {"ok": True, "event_id": event_id, "html_link": html_link}


def _forget_event_id(session_id: str) -> None:
    sidecar = _load_sidecar()
    if sidecar.pop(session_id, None) is not None:
        _save_sidecar(sidecar)


def update_calendar_event(session: Session, user_key: str = "cli") -> Dict[str, Any]:
    """Move this booking's event to the session's current slot.

    Creates the event when none was recorded or the user deleted it, so the
    calendar ends up showing the new time either way.
    """
    _refresh_host_token(session)
    cached = load_cached_token(user_key)
    if not cached or not cached.get("access_token"):
        return {
            "ok": False,
            "code": "calendar_not_connected",
            "message": "No calendar token in cache. Run calendar connect first.",
        }

    existing = existing_event_id(session.id)
    if not existing:
        return {**create_calendar_event(session, user_key=user_key), "created": True}

    event_body, err = build_event_body(session)
    if err or not event_body:
        return {"ok": False, "code": "calendar_event_invalid", "message": err or "invalid event"}

    with httpx.Client(timeout=coerce_timeout(20.0)) as client:
        resp = client.patch(
            f"{EVENTS_URL}/{existing}",
            json=event_body,
            headers={"Authorization": f"Bearer {cached['access_token']}"},
        )
    if resp.status_code in (404, 410):
        _forget_event_id(session.id)
        return {**create_calendar_event(session, user_key=user_key), "created": True}
    if resp.status_code >= 400:
        return {
            "ok": False,
            "code": "calendar_write_failed",
            "message": f"Google Calendar API error {resp.status_code}: {resp.text}",
        }

    data = resp.json()
    event_id = data.get("id") or existing
    html_link = data.get("htmlLink")
    record_event_id(session.id, event_id, html_link)
    return {"ok": True, "event_id": event_id, "html_link": html_link, "created": False}


def delete_calendar_event(session: Session, user_key: str = "cli") -> Dict[str, Any]:
    """Remove this booking's event after the booking itself was cancelled.

    Idempotent: an event the user already deleted counts as removed. A failure
    returns the saved ``html_link`` so the user can delete it by hand.
    """
    entry = _load_sidecar().get(session.id)
    entry = entry if isinstance(entry, dict) else {}
    event_id = entry.get("event_id")
    html_link = entry.get("html_link")
    if not event_id:
        return {"ok": True, "status": "skipped", "reason": "no_event"}

    _refresh_host_token(session)
    cached = load_cached_token(user_key)
    if not cached or not cached.get("access_token"):
        return {
            "ok": False,
            "status": "failed",
            "code": "calendar_not_connected",
            "event_id": event_id,
            "html_link": html_link,
        }

    try:
        with httpx.Client(timeout=coerce_timeout(20.0)) as client:
            resp = client.delete(
                f"{EVENTS_URL}/{event_id}",
                headers={"Authorization": f"Bearer {cached['access_token']}"},
            )
    except httpx.HTTPError as e:
        return {
            "ok": False,
            "status": "failed",
            "code": "calendar_delete_failed",
            "message": f"Could not reach Google Calendar ({type(e).__name__}).",
            "event_id": event_id,
            "html_link": html_link,
        }
    if resp.status_code in (200, 204):
        status = "deleted"
    elif resp.status_code in (404, 410):
        status = "already_missing"
    else:
        return {
            "ok": False,
            "status": "failed",
            "code": "calendar_delete_failed",
            "message": f"Google Calendar API error {resp.status_code}",
            "event_id": event_id,
            "html_link": html_link,
        }
    _forget_event_id(session.id)
    return {"ok": True, "status": status, "event_id": event_id}
