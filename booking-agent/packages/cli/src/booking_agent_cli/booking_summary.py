"""Booking details written by the CLI from the session, so no field can be dropped.

Used for the confirmation recap at ``buyer resolve``, the Link payment note, and
the calendar event, which must all name the same service, business, time,
address, and price.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import timezone_context as tz_context
from .catalog import business_location_address, channel_type


def selected_slot(session: Any) -> Any:
    return next(
        (s for s in session.slots_presented or [] if s.slot_id == session.slot_id), None,
    )


def service_name(session: Any) -> Optional[str]:
    live = session.live_service
    return live.get("name") if isinstance(live, dict) else None


def service_and_business(session: Any) -> str:
    service = service_name(session) or "Appointment"
    business = session.business.name if session.business else None
    return f"{service} with {business}" if business else service


def event_location(session: Any) -> Optional[str]:
    """Calendar ``location``: the service address, the business address, or the business name."""
    live = session.live_service if isinstance(session.live_service, dict) else {}
    kind = channel_type(live)
    if kind == "at_buyer_location" and session.service_address_label:
        return session.service_address_label
    if kind not in ("virtual", "phone"):
        place = business_location_address(live)
        if place:
            return place
    name = (session.business.name or "").strip() if session.business else ""
    return name or None


def where_text(session: Any) -> Optional[str]:
    """``At your address, 100 Test Street, Yuma AZ 85364``, ``Online``, or the business address."""
    live = session.live_service if isinstance(session.live_service, dict) else {}
    kind = channel_type(live)
    if kind == "at_buyer_location":
        label = session.service_address_label
        return f"At your address, {label}" if label else "At your address"
    if kind == "virtual":
        return "Online"
    if kind == "phone":
        return "By phone"
    if kind == "hybrid":
        return "Partly at the business and partly another way"
    place = business_location_address(live)
    if kind == "at_business_location" or place:
        return f"At the business, {place}" if place else "At the business"
    return None


def calendar_differs(session: Any, instants: List[Any]) -> bool:
    """True when the Google Calendar zone differs from the business zone and the buyer zone."""
    cal = tz_context.valid_iana_timezone(session.calendar_timezone)
    if cal is None or not tz_context.zones_differ(cal, session.business.timezone, instants):
        return False
    buyer = tz_context.valid_iana_timezone(session.buyer_timezone)
    return buyer is None or tz_context.zones_differ(cal, buyer, instants)


def when_text(session: Any, slot: Any, *, dual: bool, with_calendar: bool = True) -> Optional[str]:
    """Compact business-local time, with the buyer and Google Calendar times when they differ."""
    if slot is None:
        return None
    biz = session.business.timezone
    text = tz_context.compact_range_display(slot.start, slot.end, biz) or tz_context.range_display(
        slot.start, slot.end, None,
    )
    if text is None:
        return None
    extras = []
    if dual:
        buyer = tz_context.compact_clock_range(
            slot.start, slot.end, session.buyer_timezone, reference_tz=biz,
        )
        if buyer:
            extras.append(f"{buyer} your time")
    if with_calendar and calendar_differs(session, [slot.start]):
        cal = tz_context.compact_clock_range(
            slot.start, slot.end, session.calendar_timezone, reference_tz=biz,
        )
        if cal:
            extras.append(f"{cal} on your Google Calendar")
    return f"{text} ({'; '.join(extras)})" if extras else text


def price_text(session: Any, price_info: Dict[str, Any]) -> Optional[str]:
    route = session.payment_route or "at_booking"
    price = price_info.get("price_display")
    if route == "free":
        return "Free"
    if not price:
        return None
    if route in ("at_service", "pay_at_service"):
        return f"{price}, paid to the business at the appointment (nothing is charged now)"
    if route == "deposit_required":
        return (
            f"{price} total. A deposit is charged now to your Link wallet and the rest is "
            "due later; the exact deposit is shown before anything is charged"
        )
    return f"{price}, charged now to your Link wallet"


_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?)?$"
)
# Links a checkout returns that are actions, not terms the business publishes.
_ACTION_LINK_TYPES = frozenset({"continue"})


def _duration_text(value: Any) -> Optional[str]:
    """``PT24H`` -> ``24 hours``; anything else is returned as the business wrote it."""
    if not isinstance(value, str) or not value.strip():
        return None
    match = _ISO_DURATION_RE.match(value.strip().upper())
    if not match or not any(match.groupdict().values()):
        return value.strip()
    parts = []
    for unit in ("days", "hours", "minutes"):
        amount = match.group(unit)
        if amount and int(amount):
            word = unit if int(amount) != 1 else unit[:-1]
            parts.append(f"{int(amount)} {word}")
    return " ".join(parts) or "0 minutes"


def _fee_text(amount: Any, live: Dict[str, Any]) -> Optional[str]:
    if not isinstance(amount, int) or amount <= 0:
        return None
    from .catalog import _format_price_display

    pricing = live.get("pricing") if isinstance(live.get("pricing"), dict) else {}
    return _format_price_display(amount, pricing.get("currency")) or str(amount)


def service_policy_lines(live_service: Any) -> List[str]:
    """The cancellation and rescheduling policies the business published for the service.

    ``allowed`` is required, and a business with no policy configured still sends
    ``allowed: true``, so a policy that is only ``allowed: true`` with no window, fee, or
    limit says nothing the business chose and yields no line.
    """
    live = live_service if isinstance(live_service, dict) else {}
    policies = live.get("policies") if isinstance(live.get("policies"), dict) else {}
    lines: List[str] = []
    cancel = policies.get("cancellation")
    if isinstance(cancel, dict) and cancel.get("allowed") is not None:
        if cancel.get("allowed") is False:
            text = "Cancellation is not allowed"
        else:
            parts = []
            free = _duration_text(cancel.get("free_cancellation_until"))
            if free:
                parts.append(f"free up to {free} before the appointment")
            fee = _fee_text(cancel.get("late_cancellation_fee"), live)
            if fee:
                parts.append(f"a {fee} fee after that")
            closed = _duration_text(cancel.get("no_cancellation_after"))
            if closed:
                parts.append(f"no cancellation within {closed} of the appointment")
            text = f"Cancellation allowed, {', '.join(parts)}" if parts else None
        if text:
            lines.append(f"Cancellation policy: {text}.")
    resched = policies.get("rescheduling")
    if isinstance(resched, dict) and resched.get("allowed") is not None:
        if resched.get("allowed") is False:
            text = "Rescheduling is not allowed"
        else:
            parts = []
            free = _duration_text(resched.get("free_reschedule_until"))
            if free:
                parts.append(f"free up to {free} before the appointment")
            limit = resched.get("max_reschedules")
            if isinstance(limit, int) and limit >= 0:
                parts.append(f"at most {limit} time{'s' if limit != 1 else ''}")
            fee = _fee_text(resched.get("fee"), live)
            if fee:
                parts.append(f"a {fee} fee")
            text = f"Rescheduling allowed, {', '.join(parts)}" if parts else None
        if text:
            lines.append(f"Rescheduling policy: {text}.")
    return lines


def _http_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    url = value.strip()
    return url if url.startswith(("https://", "http://")) else None


def _link_label(kind: str, title: Any) -> str:
    if isinstance(title, str) and title.strip():
        return title.strip()
    words = kind.replace("_", " ").replace("-", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "Link"


def extract_policy_links(links: Any) -> List[Dict[str, str]]:
    """Terms and policy links from a checkout: no ``rel=continue`` actions, no duplicates."""
    out: List[Dict[str, str]] = []
    seen: set = set()
    for link in links or []:
        if not isinstance(link, dict):
            continue
        kind = str(link.get("type") or link.get("rel") or "").strip().lower()
        if kind in _ACTION_LINK_TYPES:
            continue
        url = _http_url(link.get("url") or link.get("href"))
        if url is None or url in seen:
            continue
        seen.add(url)
        out.append({"type": kind or "link", "label": _link_label(kind, link.get("title")), "url": url})
    return out


def _message_text(message: Dict[str, Any], zone: Optional[str]) -> Optional[str]:
    description = message.get("description")
    text = description.get("plain") if isinstance(description, dict) else None
    for key in ("content", "text", "message", "title", "name"):
        if isinstance(text, str) and text.strip():
            break
        text = message.get(key)
    if not isinstance(text, str) or not text.strip():
        return None
    return tz_context.localize_instants_in_text(text.strip(), zone)


def collect_provider_policies(session: Any) -> Dict[str, Any]:
    """Everything the business published for the user to see before the booking is final.

    ``lines`` are the service's cancellation and rescheduling policies, the checkout's policy
    texts, payment terms, and disclosures; ``links`` are the checkout's policy links (never
    ``rel=continue``). ``digest`` covers ``lines`` only: the user accepts the lines with the
    booking summary, and links a checkout adds later are shown without a new question, so a
    checkout re-asks only when it adds lines. The business website the CLI derives from the
    profile and any text the CLI writes are not included; with no lines ``digest`` is None.
    """
    zone = session.business.timezone if getattr(session, "business", None) else None
    lines = service_policy_lines(getattr(session, "live_service", None))
    links = extract_policy_links(getattr(session, "ucp_checkout_links", None))
    seen_urls = {link["url"] for link in links}
    for policy in getattr(session, "ucp_checkout_policies", None) or []:
        if not isinstance(policy, dict):
            continue
        url = _http_url(policy.get("url") or policy.get("href"))
        if url and url not in seen_urls:
            seen_urls.add(url)
            kind = str(policy.get("type") or "policy").strip().lower()
            links.append({"type": kind, "label": _link_label(kind, policy.get("title")), "url": url})
        text = _message_text(policy, zone)
        if text and not url:
            lines.append(f"Policy: {text}")
    for schedule in getattr(session, "payment_schedules", None) or []:
        if isinstance(schedule, dict):
            text = _message_text({"description": schedule.get("description")}, zone)
            if text:
                lines.append(f"Payment terms: {text}")
    for disclosure in getattr(session, "payment_disclosures", None) or []:
        if isinstance(disclosure, dict):
            text = _message_text(disclosure, zone)
            if text:
                lines.append(text)
    digest = None
    if lines:
        from .gates import digest as _digest

        digest = _digest({"lines": lines})
    return {"lines": lines, "links": links, "digest": digest}


def booking_recap_lines(
    session: Any, price_info: Dict[str, Any], *, dual: bool,
) -> List[str]:
    slot = selected_slot(session)
    buyer = session.buyer
    lines = [f"Service: {service_and_business(session)}"]
    for label, value in (
        ("When", when_text(session, slot, dual=dual)),
        ("Where", where_text(session)),
        ("Price", price_text(session, price_info)),
        ("Name", buyer.name if buyer else None),
        ("Email", buyer.email if buyer else None),
        ("Phone", buyer.phone if buyer else None),
        ("Note for the business", session.booking_notes),
    ):
        if value:
            lines.append(f"{label}: {value}")
    mismatch = getattr(session, "selection_mismatch", None)
    if mismatch:
        lines.append(f"Differs from your request: {mismatch}")
    lines.extend(service_policy_lines(session.live_service))
    return lines


def payment_context_text(session: Any, total_display: Optional[str]) -> str:
    """The Link spend-request note: what is being paid for, when, where, and how much."""
    slot = selected_slot(session)
    text = f"Booking {service_and_business(session)}"
    when = when_text(session, slot, dual=False, with_calendar=False)
    if when:
        text += f" on {when}"
    where = where_text(session)
    if where:
        text += f", {where[0].lower() + where[1:]}"
    text += "."
    if total_display:
        text += f" Total {total_display}, paid to the business through its checkout."
    text += " Booked by the booking agent at your request."
    return text


def pay_at_service_line(session: Any) -> str:
    """What is owed and when, from the business's own accepted payment term."""
    term = getattr(session, "accepted_term", None)
    schedules = term.get("schedules") if isinstance(term, dict) else None
    for schedule in schedules or []:
        description = schedule.get("description") if isinstance(schedule, dict) else None
        plain = description.get("plain") if isinstance(description, dict) else None
        if isinstance(plain, str) and plain.strip():
            return f"Nothing was charged. {plain.strip()}"
    return "Nothing was charged. Payment is due at your appointment."


def booked_message(session: Any) -> str:
    """Plain-language "you're booked" text built from saved state.

    Used by every command that can be the last word on a confirmed booking (the payment
    poll, ``book poll``, ``status``), so the user gets the same answer whichever one runs
    first, including the calendar link when an event was added.
    """
    slot = selected_slot(session)
    dual = bool(getattr(session, "timezone_difference_shown", False))
    text = f"You're booked: {service_and_business(session)}"
    when = when_text(session, slot, dual=dual) if slot is not None else None
    if when:
        text += f", {when}"
    where = where_text(session)
    if where:
        text += f". {where}"
    text = text.rstrip(".") + "."
    link = getattr(session, "calendar_event_html_link", None)
    if link:
        text += f"\nAdded to your Google Calendar: {link}"
    elif getattr(session, "calendar_skipped_unasked", False):
        text += "\nNo calendar event was added: Google Calendar was not connected for this booking."
    if getattr(session, "payment_route", None) == "pay_at_service":
        text += "\n" + pay_at_service_line(session)
    booking_url = getattr(getattr(session, "booking", None), "booking_url", None)
    if booking_url:
        text += f"\nTo view, change, or cancel it later (worth saving): {booking_url}"
    return text
