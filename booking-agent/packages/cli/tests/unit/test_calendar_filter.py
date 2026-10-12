from datetime import datetime, timezone

from booking_agent.calendar.filter import filter_slots_by_busy_times, intervals_overlap
from booking_agent.models.wire import TimeSlot


def _slot(start: str, end: str, slot_id: str = "s1") -> TimeSlot:
    return TimeSlot(
        id=slot_id,
        service_id="svc",
        start=start,
        end=end,
        duration="PT30M",
        state="available",
    )


def test_intervals_overlap_excludes_touching_boundaries():
    assert not intervals_overlap(
        "2026-06-01T14:00:00+00:00",
        "2026-06-01T14:30:00+00:00",
        "2026-06-01T14:30:00+00:00",
        "2026-06-01T15:00:00+00:00",
    )
    assert intervals_overlap(
        "2026-06-01T14:00:00+00:00",
        "2026-06-01T14:31:00+00:00",
        "2026-06-01T14:30:00+00:00",
        "2026-06-01T15:00:00+00:00",
    )


def test_filter_slots_by_busy_times_removes_overlapping_slots():
    slots = [
        _slot("2026-06-01T14:00:00+00:00", "2026-06-01T14:30:00+00:00", "a"),
        _slot("2026-06-01T14:30:00+00:00", "2026-06-01T15:00:00+00:00", "b"),
        _slot("2026-06-01T15:00:00+00:00", "2026-06-01T15:30:00+00:00", "c"),
    ]
    busy = [{"start": "2026-06-01T14:30:00+00:00", "end": "2026-06-01T15:00:00+00:00"}]
    filtered = filter_slots_by_busy_times(slots, busy)
    assert [s.id for s in filtered] == ["a", "c"]


def test_filter_slots_by_busy_times_no_busy_blocks_returns_all():
    slots = [_slot(datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc).isoformat(),
                   datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc).isoformat())]
    assert filter_slots_by_busy_times(slots, []) == slots
