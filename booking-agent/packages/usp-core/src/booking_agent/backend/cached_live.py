"""Caching live USP backend.

Talks to real USP businesses over HTTP (same as ``LiveUSPBackend``). The only
difference vs ``live`` is that this backend caches bookings it creates so later
``get_booking`` / poll calls can be served from memory if the live endpoint
hasn't finished writing the post-payment status.
"""
import logging
from typing import Any, Dict, Optional

from ..authorization import BookingScopedCredential
from ..discovery import USPBusinessProfile
from ..models.wire import (
    Booking,
    PostPaymentReturnRequest,
    USPPaymentResult,
)
from .live import LiveUSPBackend

logger = logging.getLogger(__name__)

# In-memory booking cache, keyed by booking id.
#
# Deliberately holds ``Booking`` only.  The booking-scoped credential is a
# *sibling* of the booking in a creation response, never a member of it, so it
# cannot reach this cache -- which is what we want: a credential is issuance-only
# and must not be re-served from a read path.
BOOKING_CACHE: Dict[str, Booking] = {}


class CachingLiveUSPBackend(LiveUSPBackend):
    """Live USP backend + in-memory booking cache for poll consistency."""

    async def create_booking(
        self,
        profile: USPBusinessProfile,
        service_id: str,
        slot_id: str,
        buyer: Dict[str, Any],
        party_size: int = 1,
        resource_id: Optional[str] = None,
        notes: Optional[str] = None,
        delivery_address: Optional[Dict[str, Any]] = None,
        post_payment_return_request: Optional[PostPaymentReturnRequest] = None,
    ) -> Booking:
        booking = await super().create_booking(
            profile=profile,
            service_id=service_id,
            slot_id=slot_id,
            buyer=buyer,
            party_size=party_size,
            resource_id=resource_id,
            notes=notes,
            delivery_address=delivery_address,
            post_payment_return_request=post_payment_return_request,
        )
        BOOKING_CACHE[booking.id] = booking
        return booking

    async def get_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
    ) -> Booking:
        try:
            booking = await super().get_booking(
                profile,
                booking_id,
                credential=credential,
                credential_endpoint=credential_endpoint,
            )
            BOOKING_CACHE[booking.id] = booking
            return booking
        except Exception:
            cached = BOOKING_CACHE.get(booking_id)
            if cached:
                logger.info("Returning cached booking %s (live endpoint unavailable)", booking_id)
                return cached
            raise

    async def reschedule_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        new_slot_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        booking = await super().reschedule_booking(
            profile, booking_id, new_slot_id,
            credential=credential,
            credential_endpoint=credential_endpoint,
            if_match=if_match,
        )
        BOOKING_CACHE[booking.id] = booking
        return booking

    async def confirm_payment(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        payment_result: USPPaymentResult,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        booking = await super().confirm_payment(
            profile, booking_id, payment_result,
            credential=credential, if_match=if_match,
        )
        BOOKING_CACHE[booking.id] = booking
        return booking
