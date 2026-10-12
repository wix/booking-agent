"""Per-step flow timing for V2-X5 latency budgets (#161 Track A).

Splits agent wall time, remote HTTP, merchant pacing sleep, and human gates.
Emits ``step_timing`` / ``human_gate_timing`` via ``emit_event`` (stderr JSON).
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

class _MsAccumulator:
    """Mutable counter shared across asyncio tasks in the same timed step."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0.0


_depth: ContextVar[int] = ContextVar("booking_agent_step_depth", default=0)
_active_remote_ms: ContextVar[Optional[_MsAccumulator]] = ContextVar(
    "booking_agent_step_remote_ms", default=None,
)
_active_pacing_ms: ContextVar[Optional[_MsAccumulator]] = ContextVar(
    "booking_agent_step_pacing_ms", default=None,
)

_pending: List["StepTiming"] = []


@dataclass
class StepTiming:
    step: str
    wall_ms: float
    remote_ms: float
    agent_ms: float
    pacing_ms: float
    human_gate_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "wall_ms": round(self.wall_ms, 1),
            "remote_ms": round(self.remote_ms, 1),
            "agent_ms": round(self.agent_ms, 1),
            "pacing_ms": round(self.pacing_ms, 1),
            "human_gate_ms": round(self.human_gate_ms, 1),
        }


def record_http_elapsed_ms(elapsed_ms: Optional[float]) -> None:
    """Accumulate remote HTTP time for the active step (from http_trace)."""
    if elapsed_ms is None or _depth.get() <= 0:
        return
    acc = _active_remote_ms.get()
    if acc is not None:
        acc.value += float(elapsed_ms)


def record_pacing_ms(ms: float) -> None:
    """Accumulate merchant pacing sleep for the active step."""
    if _depth.get() <= 0:
        return
    acc = _active_pacing_ms.get()
    if acc is not None:
        acc.value += float(ms)


def _emit_step_timing(timing: StepTiming) -> None:
    from booking_agent.observability import emit_event

    emit_event("step_timing", **timing.to_dict())
    _pending.append(timing)


def drain_pending_timings() -> List[StepTiming]:
    """Return and clear timings recorded since the last drain."""
    out = list(_pending)
    _pending.clear()
    return out


@asynccontextmanager
async def timed_step(step: str):
    depth_token = _depth.set(_depth.get() + 1)
    remote_acc = _MsAccumulator()
    pacing_acc = _MsAccumulator()
    remote_token = _active_remote_ms.set(remote_acc)
    pacing_token = _active_pacing_ms.set(pacing_acc)
    t0 = time.monotonic()
    try:
        yield
    finally:
        wall_ms = (time.monotonic() - t0) * 1000.0
        remote_ms = remote_acc.value
        pacing_ms = pacing_acc.value
        agent_ms = max(0.0, wall_ms - remote_ms - pacing_ms)
        _emit_step_timing(
            StepTiming(
                step=step,
                wall_ms=wall_ms,
                remote_ms=remote_ms,
                agent_ms=agent_ms,
                pacing_ms=pacing_ms,
            )
        )
        _depth.reset(depth_token)
        _active_remote_ms.reset(remote_token)
        _active_pacing_ms.reset(pacing_token)


@contextmanager
def timed_step_sync(step: str):
    depth_token = _depth.set(_depth.get() + 1)
    remote_acc = _MsAccumulator()
    pacing_acc = _MsAccumulator()
    remote_token = _active_remote_ms.set(remote_acc)
    pacing_token = _active_pacing_ms.set(pacing_acc)
    t0 = time.monotonic()
    try:
        yield
    finally:
        wall_ms = (time.monotonic() - t0) * 1000.0
        remote_ms = remote_acc.value
        pacing_ms = pacing_acc.value
        agent_ms = max(0.0, wall_ms - remote_ms - pacing_ms)
        _emit_step_timing(
            StepTiming(
                step=step,
                wall_ms=wall_ms,
                remote_ms=remote_ms,
                agent_ms=agent_ms,
                pacing_ms=pacing_ms,
            )
        )
        _depth.reset(depth_token)
        _active_remote_ms.reset(remote_token)
        _active_pacing_ms.reset(pacing_token)


@contextmanager
def timed_human_gate(gate: str):
    """Measure buyer think-time (Link approval, redirect checkout wait) separately."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        ms = (time.monotonic() - t0) * 1000.0
        from booking_agent.observability import emit_event

        emit_event(
            "human_gate_timing",
            gate=gate,
            human_gate_ms=round(ms, 1),
        )
        _pending.append(
            StepTiming(
                step=f"human_gate:{gate}",
                wall_ms=ms,
                remote_ms=0.0,
                agent_ms=0.0,
                pacing_ms=0.0,
                human_gate_ms=ms,
            )
        )
