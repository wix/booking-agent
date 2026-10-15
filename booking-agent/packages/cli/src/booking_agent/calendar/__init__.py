"""Customer calendar integration abstraction.

Strategy interface for checking the customer's personal calendar availability.
The product wires the live implementation:

    live  → booking_agent.calendar.google.GoogleCalendarService  (real Google OAuth)

Tests inject their own implementation. Selection is independent of the USP
backend.
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class CustomerCalendarService(ABC):
    """Strategy interface for customer calendar integration."""

    @abstractmethod
    async def initiate_connection(
        self, session_id: str, user_key: str = "", mode: str = "freebusy", timeout: int = 600,
    ) -> dict:
        """Start the calendar connection flow without waiting for the user.

        Returns:
            ``{"connected": True}`` if no user step is needed, or
            ``{"auth_url": "..."}`` if the user must authorize (live OAuth).
        """
        ...

    def load_cached_connection(self, session_id: str, user_key: str, mode: str) -> Optional[dict]:
        """Load an already-saved connection for ``mode`` without starting a sign-in."""
        return None

    async def poll_auth(self, session_id: str) -> dict:
        """State of the sign-in started by ``initiate_connection``.

        ``status`` is ``pending``, ``connected``, ``timeout``, ``failed``, or ``none``.
        """
        return {"status": "connected"}

    async def cleanup_auth(self) -> bool:
        """Stop a pending sign-in. True when a running listener was stopped."""
        return False

    @abstractmethod
    async def is_connected(self, session_id: str) -> bool: ...

    @abstractmethod
    async def get_busy_times(
        self, session_id: str, time_min: str, time_max: str
    ) -> List[Dict[str, str]]: ...

    async def get_calendar_timezone(self, session_id: str) -> Optional[str]:
        """IANA zone of the user's primary calendar, when the connection can read it."""
        return None
