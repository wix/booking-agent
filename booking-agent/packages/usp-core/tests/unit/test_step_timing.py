"""Tests for per-step flow timing (#161 Track A)."""
from __future__ import annotations

import asyncio

import pytest

from booking_agent.observability import reset_event_sink, set_event_sink
from booking_agent.step_timing import (
    drain_pending_timings,
    record_http_elapsed_ms,
    record_pacing_ms,
    timed_human_gate,
    timed_step,
    timed_step_sync,
)


@pytest.fixture(autouse=True)
def _isolate_events():
    captured: list = []
    set_event_sink(lambda r: captured.append(r))
    yield captured
    reset_event_sink()
    drain_pending_timings()


@pytest.mark.asyncio
async def test_timed_step_emits_step_timing():
    captured: list = []
    set_event_sink(lambda r: captured.append(r))

    async with timed_step("discover"):
        record_http_elapsed_ms(42.0)
        record_pacing_ms(10.0)

    events = [e for e in captured if e.get("event") == "step_timing"]
    assert len(events) == 1
    assert events[0]["step"] == "discover"
    assert events[0]["remote_ms"] == 42.0
    assert events[0]["pacing_ms"] == 10.0
    assert events[0]["agent_ms"] >= 0


def test_timed_step_sync_records_wall():
    captured: list = []
    set_event_sink(lambda r: captured.append(r))

    with timed_step_sync("checkout_create"):
        pass

    pending = drain_pending_timings()
    assert len(pending) == 1
    assert pending[0].step == "checkout_create"
    assert pending[0].wall_ms >= 0


def test_human_gate_separate_series():
    captured: list = []
    set_event_sink(lambda r: captured.append(r))

    with timed_human_gate("link_spend_approval"):
        pass

    gate_events = [e for e in captured if e.get("event") == "human_gate_timing"]
    assert len(gate_events) == 1
    assert gate_events[0]["gate"] == "link_spend_approval"
    pending = drain_pending_timings()
    assert pending[0].step == "human_gate:link_spend_approval"


def test_record_http_ignored_outside_step():
    record_http_elapsed_ms(100.0)
    assert drain_pending_timings() == []


@pytest.mark.asyncio
async def test_timed_step_accumulates_child_task_metrics():
    captured: list = []
    set_event_sink(lambda r: captured.append(r))

    async def child_work() -> None:
        record_http_elapsed_ms(30.0)
        record_pacing_ms(20.0)

    async with timed_step("profile_resolve"):
        await asyncio.create_task(child_work())

    events = [e for e in captured if e.get("event") == "step_timing"]
    assert len(events) == 1
    assert events[0]["remote_ms"] == 30.0
    assert events[0]["pacing_ms"] == 20.0
