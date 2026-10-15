"""Mock customer calendar service for development.

Returns a realistic week of busy intervals so the filtering is testable
across different days. No Google credentials needed.

Mock busy-block schedule (relative to today, all times UTC):
    day+1  14:30–15:00, 16:00–17:00   (overlaps business 14–17 window → filtering)
    day+2  09:00–10:30                 (morning only → no overlap with slots)
    day+3  (clean)                     (no filtering)
    day+4  15:00–16:30                 (overlaps → filtering)
    day+5  13:00–14:00                 (ends when slots start → boundary, no overlap)
    day+6  (clean)
    day+7  (clean)
"""
from datetime import date as _date, datetime, timedelta, timezone
from typing import Dict, List

from booking_agent.calendar import CustomerCalendarService


def _dt(d: _date, hour: int, minute: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc).isoformat()


def _mock_week_busy_blocks() -> List[Dict[str, str]]:
    today = datetime.now(timezone.utc).date()
    blocks: List[Dict[str, str]] = []

    d1 = today + timedelta(days=1)
    blocks.append({"start": _dt(d1, 14, 30), "end": _dt(d1, 15, 0)})
    blocks.append({"start": _dt(d1, 16, 0), "end": _dt(d1, 17, 0)})

    d2 = today + timedelta(days=2)
    blocks.append({"start": _dt(d2, 9, 0), "end": _dt(d2, 10, 30)})

    d4 = today + timedelta(days=4)
    blocks.append({"start": _dt(d4, 15, 0), "end": _dt(d4, 16, 30)})

    d5 = today + timedelta(days=5)
    blocks.append({"start": _dt(d5, 13, 0), "end": _dt(d5, 14, 0)})

    return blocks


class MockCalendarService(CustomerCalendarService):
    """Instant connection, week-based busy times sliced by requested range."""

    _connected_sessions: set = set()

    async def initiate_connection(
        self, session_id: str, user_key: str = "", mode: str = "freebusy", timeout: int = 600,
    ) -> dict:
        MockCalendarService._connected_sessions.add(session_id)
        return {"connected": True}

    def load_cached_connection(self, session_id: str, user_key: str, mode: str):
        MockCalendarService._connected_sessions.add(session_id)
        return {"connected": True}

    async def is_connected(self, session_id: str) -> bool:
        return session_id in MockCalendarService._connected_sessions

    async def get_busy_times(
        self, session_id: str, time_min: str, time_max: str
    ) -> List[Dict[str, str]]:
        range_start = datetime.fromisoformat(time_min)
        range_end = datetime.fromisoformat(time_max)
        return [
            b for b in _mock_week_busy_blocks()
            if datetime.fromisoformat(b["end"]) > range_start
            and datetime.fromisoformat(b["start"]) < range_end
        ]
