"""Filter USP time slots against customer calendar busy intervals."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Sequence

from ..models.wire import TimeSlot


def _parse_iso(s: str) -> datetime:
    """Parse an ISO8601 datetime string, handling the Z suffix for Python 3.10."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def intervals_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    """Return True when two half-open-style intervals share any interior time.

    Touching boundaries (end == start) do not count as overlap.
    """
    a_start = _parse_iso(start_a)
    a_end = _parse_iso(end_a)
    b_start = _parse_iso(start_b)
    b_end = _parse_iso(end_b)
    return a_start < b_end and b_start < a_end


def filter_slots_by_busy_times(
    slots: Sequence[TimeSlot],
    busy_blocks: List[Dict[str, str]],
) -> List[TimeSlot]:
    """Drop slots that overlap any busy block."""
    if not busy_blocks:
        return list(slots)
    return [
        slot
        for slot in slots
        if not any(
            intervals_overlap(slot.start, slot.end, busy["start"], busy["end"])
            for busy in busy_blocks
        )
    ]
