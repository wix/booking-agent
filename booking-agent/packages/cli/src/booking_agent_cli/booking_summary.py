"""Booking details written by the CLI from the session, so no field can be dropped.

Used for the confirmation recap at ``buyer resolve``, the Link payment note, and
the calendar event, which must all name the same service, business, time,
address, and price.
"""
from __future__ import annotations

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
    if route == "at_service":
        return f"{price}, paid to the business at the appointment (nothing is charged now)"
    if route == "deposit_required":
        return (
            f"{price} total. A deposit is charged now to your Link wallet and the rest is "
            "due later; the exact deposit is shown before anything is charged"
        )
    return f"{price}, charged now to your Link wallet"


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
    booking_url = getattr(getattr(session, "booking", None), "booking_url", None)
    if booking_url:
        text += f"\nTo view, change, or cancel it later (worth saving): {booking_url}"
    return text
