"""Customer calendar integration abstraction.

Strategy interface for checking the customer's personal calendar availability.
The product wires the live implementation:

    live  → booking_agent.calendar.google.GoogleCalendarService  (real Google OAuth)

Tests inject an in-memory MockCalendarService from the CLI test support tree
(packages/cli/tests/support/mock). Selection is independent of the USP backend.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class CustomerCalendarService(ABC):
    """Strategy interface for customer calendar integration."""

    @abstractmethod
    async def initiate_connection(
        self, session_id: str, user_key: str = "", mode: str = "freebusy"
    ) -> dict:
        """Start the calendar connection flow.

        Returns:
            ``{"connected": True}`` if connection is immediate (mock), or
            ``{"auth_url": "..."}`` if the user must authorize (live OAuth).
        """
        ...

    async def wait_for_auth(self, session_id: str, timeout: int = 120) -> dict:
        """Block until OAuth completes or times out (default: immediate)."""
        return {"connected": True}

    @abstractmethod
    async def is_connected(self, session_id: str) -> bool: ...

    @abstractmethod
    async def get_busy_times(
        self, session_id: str, time_min: str, time_max: str
    ) -> List[Dict[str, str]]: ...

    async def get_calendar_timezone(self, session_id: str) -> Optional[str]:
        """IANA zone of the user's primary calendar, when the connection can read it."""
        return None
