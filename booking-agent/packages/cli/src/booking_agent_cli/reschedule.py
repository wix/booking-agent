"""Reschedule rules for a CONFIRMED booking (USP §5.3.6).

This slice moves an appointment only when the move is money-neutral. The CLI
has no way to settle a price difference on reschedule, so it would leave the
buyer under- or over-charged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .booking_poll import INACTIVE_BOOKING_STATUSES
from .session import SlotRef, Session

# Same set cancel uses. ``confirmed`` is deliberately absent: unlike polling,
# a confirmed booking is exactly what gets rescheduled.
DEAD_BOOKING_STATUSES = INACTIVE_BOOKING_STATUSES


def rescheduling_in_progress(session: Session) -> bool:
    return session.reschedule_original_slot_id is not None


def clear_reschedule(session: Session) -> None:
    session.reschedule_original_slot_id = None
    session.reschedule_slots = []
    session.reschedule_slot_id = None
    session.reschedule_user_confirmed = False


def find_slot(slots: List[SlotRef], slot_id: Optional[str]) -> Optional[SlotRef]:
    if not slot_id:
        return None
    return next((s for s in slots if s.slot_id == slot_id), None)


def current_slot(session: Session) -> Optional[SlotRef]:
    return find_slot(session.slots_presented, session.slot_id)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def slot_starts_in_future(slot: Optional[SlotRef], *, now: Optional[datetime] = None) -> bool:
    start = _parse_time(slot.start if slot else None)
    return start is not None and start > (now or datetime.now(timezone.utc))


def minor_units(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def money_captured(session: Session) -> bool:
    if session.payment_route == "pay_at_service":
        return False
    return session.payment.status == "paid" or bool(session.payment.order_id)


def _service_price_minor(live_service: Optional[Dict[str, Any]]) -> Optional[int]:
    pricing = (live_service or {}).get("pricing") or {}
    return minor_units(pricing.get("amount_minor_units"))


def original_price_minor(session: Session) -> Optional[int]:
    """Effective price of the booked slot.

    A slot without its own pricing inherits the service price. The checkout
    total is the last resort because it can include tax.
    """
    slot = find_slot(session.slots_presented, session.reschedule_original_slot_id or session.slot_id)
    if slot is not None and slot.price_amount is not None:
        return slot.price_amount
    service_price = _service_price_minor(session.live_service)
    if service_price is not None:
        return service_price
    return session.ucp_checkout_amount_minor


def candidate_price_minor(slot_price: Optional[int], live_service: Optional[Dict[str, Any]]) -> Optional[int]:
    return slot_price if slot_price is not None else _service_price_minor(live_service)


def price_refusal(session: Session, candidate: SlotRef) -> Optional[Tuple[str, str]]:
    """``(code, message)`` when moving to *candidate* would change what is owed."""
    original = original_price_minor(session)
    new = candidate.price_amount
    if original is None or new is None:
        if money_captured(session):
            return (
                "reschedule_price_unknown",
                "The price of the booked slot or the new slot is unknown, and this booking "
                "was already paid. Moving it could leave the payment wrong, so it cannot be "
                "moved here.",
            )
        return None
    if original != new:
        return (
            "reschedule_price_changed",
            f"The new slot costs {new} (minor units) and the booked slot cost {original}. "
            "Only moves to a slot with the same price are supported here.",
        )
    return None


def apply_rescheduled_booking(session: Session, booking: Any, chosen: SlotRef) -> SlotRef:
    """Point the session at the slot the business accepted and drop working state."""
    server_slot = getattr(booking, "slot", None)
    new_ref = SlotRef(
        slot_id=getattr(server_slot, "id", None) or chosen.slot_id,
        start=getattr(server_slot, "start", None) or chosen.start,
        end=getattr(server_slot, "end", None) or chosen.end,
        duration=getattr(server_slot, "duration", None) or chosen.duration,
        price_amount=chosen.price_amount,
    )
    replaced = {session.slot_id, new_ref.slot_id}
    session.slots_presented = [
        s for s in session.slots_presented if s.slot_id not in replaced
    ] + [new_ref]
    session.slot_id = new_ref.slot_id
    revision = getattr(booking, "revision", None)
    if revision:
        session.booking.revision = revision
    booking_url = getattr(booking, "booking_url", None)
    if booking_url:
        session.booking.booking_url = booking_url
    clear_reschedule(session)
    return new_ref
