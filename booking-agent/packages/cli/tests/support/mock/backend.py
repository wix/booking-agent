"""In-memory USP backend for CLI tests.

Deterministic registry search (no LLM). All catalog, availability, and booking
operations are served from the bundled fixtures and an in-memory booking store.
``create_booking`` returns a payment action whose ``continue_url`` points at
``MOCK_CHECKOUT_PORT``; no server is started. ``mark_booking_paid`` makes a later
``get_booking`` report the booking as confirmed.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from booking_agent.authorization import BookingScopedCredential
from booking_agent.backend import USPBackend
from booking_agent.discovery import USPBusinessProfile
from booking_agent.models.platform import ServiceSearchIntent
from booking_agent.models.wire import (
    Action,
    AvailabilityResult,
    ActionStatus,
    Booking,
    BookingLocation,
    BookingPayment,
    BookingPaymentStatus,
    BookingPaymentTiming,
    BookingResource,
    BookingStatus,
    Buyer,
    DeliveryAddress,
    Hold,
    HoldStatus,
    LineItem,
    PaymentContextMetadata,
    PostPaymentReturnRequest,
    SlotReference,
    SlotResource,
    TimeSlot,
    USPPaymentContext,
    USPPaymentResult,
    USPService,
)

from ..fixtures import MOCK_PROFILES, MOCK_SERVICES, SERVICE_BY_ID

logger = logging.getLogger(__name__)

MOCK_CHECKOUT_PORT = 8099

BOOKINGS: Dict[str, Booking] = {}
POST_PAYMENT_RETURN_REQUESTS: Dict[str, PostPaymentReturnRequest] = {}
_PAID: set = set()


def mark_booking_paid(booking_id: str) -> None:
    _PAID.add(booking_id)


def is_booking_paid(booking_id: str) -> bool:
    return booking_id in _PAID


def reset_paid() -> None:
    _PAID.clear()


def _hostname(url: str) -> str:
    return urlparse(url).hostname or "unknown"


def _score_service(service: USPService, query: str) -> int:
    q = query.lower()
    score = 0
    if q in (service.name or "").lower():
        score += 5
    if service.description:
        desc = service.description if isinstance(service.description, str) else service.description.plain
        if q in (desc or "").lower():
            score += 2
    if service.category and q in (service.category.name or "").lower():
        score += 2
    elif service.categories:
        for entry in service.categories:
            if q in (entry.name or entry.value or "").lower():
                score += 2
                break
    for tok in q.split():
        if tok and tok in (service.name or "").lower():
            score += 1
    if service.business and q in (service.business.name or "").lower():
        score += 1
    return score


def _build_mock_slots(service: USPService, start_date: str, end_date: str) -> List[TimeSlot]:
    duration_minutes = 30
    if service.duration.fixed:
        iso = service.duration.fixed
        if "M" in iso:
            try:
                duration_minutes = int(iso.replace("PT", "").replace("M", ""))
            except ValueError:
                duration_minutes = 30
    try:
        range_start = datetime.fromisoformat(start_date).date()
    except (ValueError, TypeError):
        range_start = datetime.now(timezone.utc).date() + timedelta(days=1)
    try:
        range_end = datetime.fromisoformat(end_date).date()
    except (ValueError, TypeError):
        range_end = range_start

    slot_resources: List[SlotResource] = []
    if service.resources:
        for r in service.resources:
            if r.type == "staff" and r.options:
                for opt in r.options:
                    slot_resources.append(SlotResource(id=opt.id, type="staff", name=opt.name))

    slots: List[TimeSlot] = []
    slot_num = 0
    current = range_start
    while current <= range_end:
        hour, minute = 14, 0
        while True:
            start = datetime(current.year, current.month, current.day, hour, minute, tzinfo=timezone.utc)
            end = start + timedelta(minutes=duration_minutes)
            if end.hour > 17 or (end.hour == 17 and end.minute > 0):
                break
            slot_num += 1
            slots.append(
                TimeSlot(
                    id=f"{service.id}_slot_{slot_num:02d}",
                    service_id=service.id,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    duration=service.duration.fixed or f"PT{duration_minutes}M",
                    state="available",
                    resources=slot_resources or None,
                )
            )
            minute += duration_minutes
            while minute >= 60:
                hour += 1
                minute -= 60
        current += timedelta(days=1)
    return slots


class MockUSPBackend(USPBackend):
    """Development backend returning hardcoded data for all USP operations."""

    # -- Registry-level ------------------------------------------------------

    async def search_registry(self, intent: ServiceSearchIntent) -> List[USPService]:
        ranked = sorted(
            ((s, _score_service(s, intent.query)) for s in MOCK_SERVICES),
            key=lambda pair: pair[1],
            reverse=True,
        )
        scored = [s for s, score in ranked if score > 0]
        if not scored:
            scored = list(MOCK_SERVICES)
        services: List[USPService] = []
        for rank, service in enumerate(scored[:3]):
            meta = dict(service.metadata or {})
            meta["search_rank"] = rank
            services.append(service.model_copy(update={"metadata": meta}))
        return services

    # -- Discovery -----------------------------------------------------------

    async def resolve_profile(self, usp_profile_url: str) -> USPBusinessProfile:
        host = _hostname(usp_profile_url)
        profile = MOCK_PROFILES.get(host)
        if profile:
            return profile
        raise RuntimeError(
            f"No mock USP profile for hostname '{host}' (url={usp_profile_url}). "
            "Add an entry to tests/support/mock/fixtures/profiles.py MOCK_PROFILES."
        )

    async def resolve_profile_from_agent_card(self, agent_card_url: str) -> USPBusinessProfile:
        host = _hostname(agent_card_url)
        profile = MOCK_PROFILES.get(host)
        if profile:
            return profile
        raise RuntimeError(
            f"No mock USP profile for hostname '{host}' (agent_card_url={agent_card_url})."
        )

    # -- Catalog -------------------------------------------------------------

    async def list_services(
        self, profile: USPBusinessProfile, filters: Optional[Dict[str, Any]] = None
    ) -> List[USPService]:
        biz_name = profile.business.name
        matches = [
            s for s in MOCK_SERVICES
            if s.business and s.business.name == biz_name
        ]
        return matches or list(MOCK_SERVICES)

    async def get_service(self, profile: USPBusinessProfile, service_id: str) -> USPService:
        svc = SERVICE_BY_ID.get(service_id)
        if not svc:
            raise RuntimeError(f"Mock service not found: {service_id}")
        return svc

    # -- Availability --------------------------------------------------------

    async def query_availability(
        self,
        profile: USPBusinessProfile,
        service_id: str,
        start_date: str,
        end_date: str,
        timezone: Optional[str] = None,
    ) -> AvailabilityResult:
        # Tests pin the business zone on the session, which the client sends as
        # date_bounds_timezone; the shared mock profile only says UTC.
        zone = timezone or profile.business.timezone
        service = SERVICE_BY_ID.get(service_id)
        slots = _build_mock_slots(service, start_date, end_date) if service else []
        return AvailabilityResult(
            slots, business_timezone=zone, applied_date_bounds_timezone=zone,
        )

    async def hold_slot(
        self, profile: USPBusinessProfile, slot_id: str, service_id: str, spots: int = 1
    ) -> Hold:
        return Hold(
            id=f"hold_{uuid.uuid4().hex[:8]}",
            slot_id=slot_id,
            service_id=service_id,
            spots=spots,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            status=HoldStatus.ACTIVE,
        )

    async def release_hold(
        self,
        profile: USPBusinessProfile,
        hold_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
    ) -> dict:
        return {"hold_id": hold_id, "status": "released"}

    # -- Booking -------------------------------------------------------------

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
        service = SERVICE_BY_ID.get(service_id)
        if not service:
            raise RuntimeError(f"Mock service not found: {service_id}")

        now = datetime.now(timezone.utc)
        slot = self._find_slot(service, slot_id)

        resources = None
        if resource_id and service.resources:
            for req in service.resources:
                if req.options:
                    for opt in req.options:
                        if opt.id == resource_id:
                            resources = [BookingResource(id=opt.id, type=req.type, name=opt.name)]
                            break

        location = None
        if service.locations:
            loc = service.locations[0]
            location = BookingLocation(id=loc.id, name=loc.name)

        parsed_delivery = (
            DeliveryAddress.model_validate(delivery_address)
            if delivery_address
            else None
        )

        requires_payment = service.policies.requires_payment
        status = BookingStatus.REQUIRES_ACTION if requires_payment else BookingStatus.CONFIRMED
        booking_id = f"bkg_{uuid.uuid4().hex[:8]}"

        payment = None
        actions = None
        if requires_payment:
            payment = BookingPayment(
                status=BookingPaymentStatus.PENDING,
                timing=BookingPaymentTiming.AT_BOOKING,
                amount=service.pricing.amount,
                currency=service.pricing.currency,
                amount_due=service.pricing.amount,
            )
            payment_ctx = USPPaymentContext(
                amount_due=service.pricing.amount or 0,
                currency=service.pricing.currency,
                description=f"Payment for {service.name}",
                line_items=[
                    LineItem(
                        label=service.name,
                        amount=service.pricing.amount or 0,
                        quantity=party_size,
                        item_id=service.id,
                    )
                ],
                metadata=PaymentContextMetadata(
                    booking_id=booking_id,
                    service_id=service.id,
                    service_type=service.type,
                    slot_start=slot.start if slot else None,
                ),
            )
            actions = [
                Action(
                    type="payment",
                    status=ActionStatus.PENDING,
                    continue_url=f"http://127.0.0.1:{MOCK_CHECKOUT_PORT}/session/{booking_id}",
                    expires_at=(now + timedelta(minutes=15)).isoformat(),
                    payment_context=payment_ctx,
                )
            ]

        booking = Booking(
            id=booking_id,
            service_id=service.id,
            service_name=service.name,
            slot=SlotReference(
                id=slot_id,
                start=slot.start if slot else now.isoformat(),
                end=slot.end if slot else (now + timedelta(minutes=30)).isoformat(),
                duration=slot.duration if slot else "PT30M",
            ),
            buyer=Buyer(**buyer),
            party_size=party_size,
            resources=resources,
            location=location,
            delivery_address=parsed_delivery,
            status=status,
            confirmation_mode=service.policies.confirmation_mode,
            payment=payment,
            actions=actions,
            notes=notes,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        BOOKINGS[booking_id] = booking
        if post_payment_return_request:
            POST_PAYMENT_RETURN_REQUESTS[booking_id] = post_payment_return_request
        return booking

    async def get_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
    ) -> Booking:
        booking = BOOKINGS.get(booking_id)
        if not booking:
            raise RuntimeError(f"Mock booking not found: {booking_id}")
        if not is_booking_paid(booking_id):
            return booking
        return booking.model_copy(update={
            "status": BookingStatus.CONFIRMED,
            "payment": BookingPayment(
                status=BookingPaymentStatus.PAID,
                timing=BookingPaymentTiming.AT_BOOKING,
                amount=booking.payment.amount if booking.payment else 0,
                currency=booking.payment.currency if booking.payment else "USD",
                transaction_id=f"txn_{booking_id}",
            ),
        })

    async def cancel_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        reason: Optional[str] = None,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        booking = BOOKINGS.get(booking_id)
        now = datetime.now(timezone.utc)
        if booking:
            cancelled = booking.model_copy(update={
                "status": BookingStatus.CANCELED,
                "updated_at": now.isoformat(),
            })
            BOOKINGS[booking_id] = cancelled
            return cancelled
        raise RuntimeError(f"Mock booking not found: {booking_id}")

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
        booking = BOOKINGS.get(booking_id)
        if not booking:
            raise RuntimeError(f"Mock booking not found: {booking_id}")
        service = SERVICE_BY_ID.get(booking.service_id)
        slot = self._find_slot(service, new_slot_id) if service else None
        now = datetime.now(timezone.utc)
        new_ref = (
            SlotReference(id=slot.id, start=slot.start, end=slot.end, duration=slot.duration)
            if slot
            else booking.slot.model_copy(update={"id": new_slot_id})
        )
        moved = booking.model_copy(update={"slot": new_ref, "updated_at": now.isoformat()})
        BOOKINGS[booking_id] = moved
        return moved

    async def confirm_payment(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        payment_result: USPPaymentResult,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        booking = BOOKINGS.get(booking_id)
        if not booking:
            raise RuntimeError(f"Mock booking not found: {booking_id}")
        now = datetime.now(timezone.utc)
        updated = booking.model_copy(update={
            "status": BookingStatus.CONFIRMED,
            "payment": BookingPayment(
                status=BookingPaymentStatus.PAID,
                timing=BookingPaymentTiming.AT_BOOKING,
                amount=payment_result.amount_paid,
                currency=payment_result.currency,
                transaction_id=payment_result.transaction_id,
            ),
            "updated_at": now.isoformat(),
        })
        BOOKINGS[booking_id] = updated
        return updated

    # -- helpers -------------------------------------------------------------

    def _find_slot(self, service: USPService, slot_id: str) -> Optional[TimeSlot]:
        now = datetime.now(timezone.utc)
        start = now.isoformat()
        end = (now + timedelta(days=7)).isoformat()
        for slot in _build_mock_slots(service, start, end):
            if slot.id == slot_id:
                return slot
        return None
