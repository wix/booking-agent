from datetime import datetime, timedelta, timezone

import pytest

from booking_agent.discovery import USPBusinessInfo, USPBusinessProfile
from booking_agent.headless import query_availability_with_quirk
from booking_agent.models.wire import TimeSlot


class _StubBackend:
    """Records calls; returns a configurable sequence of result lists."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def query_availability(
        self, profile, service_id, start_date, end_date, timezone_str=None
    ):
        self.calls.append((start_date, end_date))
        return self._responses.pop(0) if self._responses else []


def _slot(svc: str, start: datetime, duration_min: int = 30) -> TimeSlot:
    end = start + timedelta(minutes=duration_min)
    return TimeSlot(
        id=f"{svc}_{start.isoformat()}",
        service_id=svc,
        start=start.isoformat(),
        end=end.isoformat(),
        duration=f"PT{duration_min}M",
        state="available",
    )


def _profile() -> USPBusinessProfile:
    return USPBusinessProfile(
        version="1.0",
        services={},
        capabilities={},
        checkout_systems=[],
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )


@pytest.mark.asyncio
async def test_single_day_empty_retries_with_range_and_filters():
    target = datetime(2026, 6, 4, 0, 0, tzinfo=timezone.utc)
    wider_slot = _slot("svc1", target + timedelta(hours=14))
    other_day_slot = _slot("svc1", target + timedelta(days=2, hours=14))
    backend = _StubBackend([[], [wider_slot, other_day_slot]])

    start = target.isoformat()
    end = (target + timedelta(hours=23, minutes=59)).isoformat()

    slots = await query_availability_with_quirk(
        backend, _profile(), "svc1", start, end
    )

    assert len(slots) == 1
    assert slots[0].id == wider_slot.id
    assert len(backend.calls) == 2


@pytest.mark.asyncio
async def test_multi_day_window_does_not_retry():
    backend = _StubBackend([[]])
    start = "2026-06-04T00:00:00+00:00"
    end = "2026-06-08T23:59:59+00:00"
    slots = await query_availability_with_quirk(backend, _profile(), "svc1", start, end)
    assert slots == []
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_date_only_single_day_retries_as_dates_and_filters_by_applied_zone():
    from booking_agent.models.wire import AvailabilityResult

    # 23:30 UTC on Jun 3 is Jun 4 in Jerusalem (UTC+3); 22:00 UTC on Jun 4 is Jun 5 there.
    on_day = _slot("svc1", datetime(2026, 6, 3, 23, 30, tzinfo=timezone.utc))
    next_day = _slot("svc1", datetime(2026, 6, 4, 22, 0, tzinfo=timezone.utc))
    wide = AvailabilityResult(
        [on_day, next_day],
        business_timezone="Asia/Jerusalem",
        applied_date_bounds_timezone="Asia/Jerusalem",
    )
    backend = _StubBackend([[], wide])
    slots = await query_availability_with_quirk(
        backend, _profile(), "svc1", "2026-06-04", "2026-06-04",
    )
    assert backend.calls == [("2026-06-04", "2026-06-04"), ("2026-06-04", "2026-06-10")]
    assert [s.id for s in slots] == [on_day.id]
    assert slots.business_timezone == "Asia/Jerusalem"
    assert slots.applied_date_bounds_timezone == "Asia/Jerusalem"


@pytest.mark.asyncio
async def test_date_only_retry_without_applied_zone_filters_by_slot_offset():
    from booking_agent.models.wire import AvailabilityResult

    on_day = TimeSlot(
        id="a", service_id="svc1", start="2026-06-04T23:30:00-07:00",
        end="2026-06-05T00:30:00-07:00", duration="PT1H", state="available",
    )
    next_day = TimeSlot(
        id="b", service_id="svc1", start="2026-06-05T09:00:00-07:00",
        end="2026-06-05T10:00:00-07:00", duration="PT1H", state="available",
    )
    backend = _StubBackend([[], AvailabilityResult([on_day, next_day])])
    slots = await query_availability_with_quirk(
        backend, _profile(), "svc1", "2026-06-04", "2026-06-04",
    )
    assert [s.id for s in slots] == ["a"]
    assert slots.business_timezone is None
