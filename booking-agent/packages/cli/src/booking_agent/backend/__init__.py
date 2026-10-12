"""USP Backend abstraction layer.

Single interface (USPBackend) for all USP operations — registry search,
profile resolution, catalog, availability, and booking. The product
implementation is booking_agent.backend.live.LiveUSPBackend; tests inject
their own.
"""
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone


def _parse_iso(s: str) -> datetime:
    """Parse an ISO8601 datetime string, handling the Z suffix for Python 3.10."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

from typing import Any, Dict, List, Optional

from ..authorization import BookingScopedCredential
from ..discovery import USPBusinessProfile
from ..models.platform import ServiceSearchIntent
from ..models.wire import (
    Booking,
    Hold,
    TimeSlot,
    USPPaymentResult,
    USPService,
)

logger = logging.getLogger(__name__)


class USPBackend(ABC):
    """Strategy interface for all USP operations."""

    #: Set by the resource-creating operations (``hold_slot``, ``create_booking``)
    #: to the ``booking_scoped_credential`` the business issued, already verified
    #: against this agent's key.  Read it immediately after the call: creation
    #: responses are the only place a credential ever appears, and the next
    #: creation overwrites it.  ``None`` when the business issued none.
    last_issued_credential: Optional[BookingScopedCredential] = None

    # -- Registry-level (service discovery) ----------------------------------

    @abstractmethod
    async def search_registry(self, intent: ServiceSearchIntent) -> List[USPService]: ...

    # -- Discovery (profile resolution) --------------------------------------

    @abstractmethod
    async def resolve_profile(self, usp_profile_url: str) -> USPBusinessProfile: ...

    @abstractmethod
    async def resolve_profile_from_agent_card(
        self, agent_card_url: str
    ) -> USPBusinessProfile: ...

    # -- Business-level (catalog) --------------------------------------------

    @abstractmethod
    async def list_services(
        self,
        profile: USPBusinessProfile,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[USPService]: ...

    @abstractmethod
    async def get_service(
        self, profile: USPBusinessProfile, service_id: str
    ) -> USPService: ...

    # -- Business-level (availability) ---------------------------------------

    @abstractmethod
    async def query_availability(
        self,
        profile: USPBusinessProfile,
        service_id: str,
        start_date: str,
        end_date: str,
        timezone: Optional[str] = None,
    ) -> List[TimeSlot]:
        """Slots for the window. ``timezone`` is sent as ``date_bounds_timezone``.

        Live backends return an ``AvailabilityResult`` carrying the response's
        ``business_timezone`` and ``applied_date_bounds_timezone``.
        """

    async def find_next_available(
        self,
        profile: USPBusinessProfile,
        service_id: str,
        after_date: str,
        max_days: int = 30,
        timezone_str: Optional[str] = None,
    ) -> Optional[str]:
        try:
            base = _parse_iso(after_date).date()
        except (ValueError, TypeError):
            base = datetime.now(timezone.utc).date()
        start_day = base + timedelta(days=1)
        end_day = base + timedelta(days=max_days)
        try:
            slots = await self.query_availability(
                profile, service_id,
                f"{start_day.isoformat()}T00:00:00Z",
                f"{end_day.isoformat()}T23:59:59Z",
                timezone_str,
            )
        except Exception:
            logger.debug("find_next_available: query failed for %s", service_id)
            return None
        if not slots:
            return None
        earliest = min(slots, key=lambda s: s.start)
        return _parse_iso(earliest.start).date().isoformat()

    @abstractmethod
    async def hold_slot(
        self,
        profile: USPBusinessProfile,
        slot_id: str,
        service_id: str,
        spots: int = 1,
    ) -> Hold: ...

    @abstractmethod
    async def release_hold(
        self,
        profile: USPBusinessProfile,
        hold_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
    ) -> dict: ...

    # -- Business-level (booking) --------------------------------------------

    @abstractmethod
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
    ) -> Booking: ...

    @abstractmethod
    async def get_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
    ) -> Booking: ...

    @abstractmethod
    async def cancel_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        reason: Optional[str] = None,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> Booking: ...

    @abstractmethod
    async def reschedule_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        new_slot_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> Booking: ...

    @abstractmethod
    async def confirm_payment(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        payment_result: USPPaymentResult,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking: ...


def get_usp_backend() -> USPBackend:
    from .live import LiveUSPBackend
    return LiveUSPBackend()


# ISO 4217 minor unit exponent. Default 2.
CURRENCY_MINOR_UNITS = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0,
    "KRW": 0, "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}

CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩"}


def minor_to_decimal(amount: int, currency: str) -> float:
    exponent = CURRENCY_MINOR_UNITS.get(currency.upper(), 2)
    return amount / (10 ** exponent)
