"""Persist per-step flow timings on the session and surface them in CLI output."""
from __future__ import annotations

from typing import Any, Dict

from booking_agent.step_timing import drain_pending_timings, StepTiming

from .session import Session


def apply_pending_timings(session: Session) -> None:
    """Merge drained step timings into the session record."""
    for timing in drain_pending_timings():
        if timing.step.startswith("human_gate:"):
            gate = timing.step.split(":", 1)[1]
            session.human_gate_timings[gate] = round(timing.human_gate_ms, 1)
        else:
            session.flow_timings[timing.step] = timing.to_dict()


def timings_data(session: Session) -> Dict[str, Any]:
    """Payload fragment for AgentOutput ``data``."""
    return {
        "flow_timings": dict(session.flow_timings),
        "human_gate_timings": dict(session.human_gate_timings),
    }


def record_timing_on_session(session: Session, timing: StepTiming) -> None:
    if timing.step.startswith("human_gate:"):
        gate = timing.step.split(":", 1)[1]
        session.human_gate_timings[gate] = round(timing.human_gate_ms, 1)
    else:
        session.flow_timings[timing.step] = timing.to_dict()
