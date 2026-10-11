"""Booking-status polling helpers shared by flow commands.

Provides an in-process blocking wait for a booking to reach a terminal status,
so agents do not have to loop ``booking-agent flow book poll`` across harness turns.
An optional ``return_event`` (set by the local post-payment return listener)
lets the wait poll immediately instead of sleeping the full interval.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Optional

from .engine import transition
from .secrets.demo import resolve_backend
from .session import INACTIVE_BOOKING_STATUSES, Session, credential_for_session_resource

from booking_agent.observability import emit_event, money_path_fields

# Statuses that end confirmation polling. ``confirmed`` is success for that wait.
TERMINAL_BOOKING_STATUSES = {"confirmed", "completed", "canceled", "no_show"}
_SUCCESS_STATUSES = {"confirmed", "completed"}
_FAILURE_STATUSES = {"canceled", "no_show"}


class BookingPollTimeout(RuntimeError):
    """Raised when a booking does not reach a terminal status before the deadline."""


def emit_payment_unconfirmed_on_poll_timeout(session: Session) -> None:
    """Charge may have succeeded while booking confirmation did not finish."""
    if (
        session.payment.status in {"paid", "pending"}
        or session.payment.spend_request_id
    ):
        emit_event(
            "payment_captured_booking_unconfirmed",
            reason="poll_timeout",
            **money_path_fields(
                spend_request_id=session.payment.spend_request_id,
                booking_id=session.booking.id,
            ),
        )


def booking_status_value(booking: Any) -> str:
    status = getattr(booking, "status", booking)
    return status.value if hasattr(status, "value") else str(status)


def apply_booking_status(session: Session, booking: Any) -> str:
    """Apply session/payment transitions for the booking's status; return status string."""
    # Refresh the concurrency token on every read.  A revision captured at
    # creation goes stale the moment anything mutates the booking -- including
    # the business itself confirming a payment -- and a stale one turns the next
    # conditional write into a 412.
    revision = getattr(booking, "revision", None)
    if revision:
        session.booking.revision = revision
    status_val = booking_status_value(booking)
    session.booking.status = status_val
    if status_val in _SUCCESS_STATUSES:
        session.payment.status = "paid"
        transition(session, to="CONFIRMED")
    elif status_val in _FAILURE_STATUSES:
        if session.payment.spend_request_id or session.payment.status in {"paid", "pending"}:
            emit_event(
                "confirmation_failed",
                booking_status=status_val,
                **money_path_fields(
                    spend_request_id=session.payment.spend_request_id,
                    booking_id=session.booking.id,
                ),
            )
        session.payment.status = "failed"
        transition(session, to="FAILED")
    else:
        session.payment.status = session.payment.status or "pending"
    return status_val


async def _fetch_booking_async(session: Session, booking_id: str) -> Any:
    backend = resolve_backend()
    profile = await backend.resolve_profile(session.business.profile_url)
    # ``usp_bookings_get`` is privileged_scoped: it returns buyer personal data,
    # so it carries the credential like any other scoped call.
    credential = credential_for_session_resource(
        session,
        endpoint=profile.rest_endpoint,
        resolved_business_endpoint=profile.rest_endpoint,
    )
    return await backend.get_booking(
        profile,
        booking_id,
        credential=credential,
        credential_endpoint=session.booking.endpoint if credential is not None else None,
    )


def fetch_booking(session: Session, booking_id: str) -> Any:
    """Single ``get_booking`` call (resolves the profile each time, like book_poll)."""
    return asyncio.run(_fetch_booking_async(session, booking_id))


def wait_for_booking_terminal(
    session: Session,
    *,
    deadline_ts: float,
    poll_interval: float,
    return_event: Optional[threading.Event] = None,
) -> Any:
    """Block until the booking reaches a terminal status or the deadline passes.

    When ``return_event`` is provided, a set event wakes the loop early so the
    next ``get_booking`` happens immediately rather than after the full interval.
    Raises ``BookingPollTimeout`` if the deadline is reached first.
    """
    booking_id = session.booking.id
    if not booking_id:
        raise BookingPollTimeout("No booking_id in session to poll.")

    while True:
        booking = fetch_booking(session, booking_id)
        status_val = booking_status_value(booking)
        if status_val in TERMINAL_BOOKING_STATUSES:
            return booking

        remaining = deadline_ts - time.time()
        if remaining <= 0:
            raise BookingPollTimeout(
                f"Booking {booking_id} still '{status_val}' after timeout."
            )

        wait_for = min(poll_interval, remaining)
        if return_event is not None:
            # Wake early if the merchant redirect hit the local return listener.
            if return_event.wait(timeout=wait_for):
                return_event.clear()
        else:
            time.sleep(wait_for)
