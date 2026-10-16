#!/usr/bin/env python3
"""Per-step latency and soak harness for the booking flow.

Consumes ``step_timing`` stderr events from normal flow commands or direct
backend calls. Reports p50/p95 per step and exits non-zero on budget breach.

Hermetic mode times synthetic in-process steps and needs no network. Staging
mode uses the public Wix USP registry by default; ``USP_REGISTRY_URL`` may
override it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from booking_agent.registry_url import DEFAULT_REGISTRY_URL

REPO_ROOT = Path(__file__).resolve().parents[1]

# Published budgets (p50/p95 ms). Reconcile with X2 deadlines in ops/budgets.md.
DEFAULT_BUDGETS: Dict[str, Dict[str, float]] = {
    "discover": {"p50": 800.0, "p95": 15000.0},
    "profile_resolve": {"p50": 3000.0, "p95": 30000.0},
    "availability": {"p50": 2000.0, "p95": 15000.0},
    "checkout_create": {"p50": 3000.0, "p95": 60000.0},
    "pay": {"p50": 5000.0, "p95": 90000.0, "p99": 90000.0},
    "confirm": {"p50": 2000.0, "p95": 120000.0},
}

SERVICE_STEPS = frozenset(
    {"discover", "profile_resolve", "availability", "checkout_create", "pay", "confirm"}
)


@dataclass
class Sample:
    step: str
    wall_ms: float
    remote_ms: float
    agent_ms: float
    pacing_ms: float
    human_gate_ms: float = 0.0
    correlation_id: Optional[str] = None


@dataclass
class RunReport:
    mode: str
    iterations: int
    concurrency: int
    registry_url: Optional[str] = None
    query: str = "haircut"
    samples: List[Sample] = field(default_factory=list)
    human_gate_samples: List[Sample] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def add_event(self, record: Dict[str, Any]) -> None:
        event = record.get("event")
        if event == "step_timing":
            step = str(record.get("step", ""))
            if step.startswith("human_gate:"):
                self.human_gate_samples.append(
                    Sample(
                        step=step,
                        wall_ms=float(record.get("wall_ms", 0)),
                        remote_ms=0.0,
                        agent_ms=0.0,
                        pacing_ms=0.0,
                        human_gate_ms=float(record.get("human_gate_ms", record.get("wall_ms", 0))),
                        correlation_id=record.get("correlation_id"),
                    )
                )
                return
            self.samples.append(
                Sample(
                    step=step,
                    wall_ms=float(record.get("wall_ms", 0)),
                    remote_ms=float(record.get("remote_ms", 0)),
                    agent_ms=float(record.get("agent_ms", 0)),
                    pacing_ms=float(record.get("pacing_ms", 0)),
                    correlation_id=record.get("correlation_id"),
                )
            )
        elif event == "human_gate_timing":
            gate = str(record.get("gate", "unknown"))
            self.human_gate_samples.append(
                Sample(
                    step=f"human_gate:{gate}",
                    wall_ms=float(record.get("human_gate_ms", 0)),
                    remote_ms=0.0,
                    agent_ms=0.0,
                    pacing_ms=0.0,
                    human_gate_ms=float(record.get("human_gate_ms", 0)),
                    correlation_id=record.get("correlation_id"),
                )
            )
        elif event == "registry_search" and record.get("ok"):
            self.samples.append(
                Sample(
                    step="discover",
                    wall_ms=float(record.get("latency_ms", 0)),
                    remote_ms=float(record.get("latency_ms", 0)),
                    agent_ms=0.0,
                    pacing_ms=0.0,
                    correlation_id=record.get("correlation_id"),
                )
            )


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def aggregate(samples: List[Sample], step: str) -> Dict[str, float]:
    rows = [s for s in samples if s.step == step and s.human_gate_ms == 0]
    walls = [s.wall_ms for s in rows]
    remotes = [s.remote_ms for s in rows]
    if not walls:
        return {}
    return {
        "n": len(walls),
        "p50_wall_ms": round(percentile(walls, 50), 1),
        "p95_wall_ms": round(percentile(walls, 95), 1),
        "p50_remote_ms": round(percentile(remotes, 50), 1),
        "p95_remote_ms": round(percentile(remotes, 95), 1),
    }


def check_budgets(
    report: RunReport,
    budgets: Dict[str, Dict[str, float]],
) -> List[str]:
    breaches: List[str] = []
    for step, limits in budgets.items():
        agg = aggregate(report.samples, step)
        if not agg:
            continue
        p95 = agg.get("p95_wall_ms", 0)
        cap = limits.get("p95")
        if cap is not None and p95 > cap:
            breaches.append(f"{step} p95_wall_ms={p95} exceeds budget p95={cap}")
        if step == "pay" and limits.get("p99") is not None:
            walls = [s.wall_ms for s in report.samples if s.step == step]
            p99 = percentile(walls, 99) if walls else 0
            if p99 > limits["p99"]:
                breaches.append(f"pay p99_wall_ms={round(p99,1)} exceeds budget p99={limits['p99']}")
    return breaches


def print_report(report: RunReport, budgets: Dict[str, Dict[str, float]]) -> None:
    print(f"mode={report.mode} iterations={report.iterations} concurrency={report.concurrency}")
    if report.registry_url:
        print(f"registry_url={report.registry_url}")
    print(f"query={report.query} timestamp={report.timestamp}")
    print("--- service steps (human gates excluded) ---")
    for step in sorted(SERVICE_STEPS):
        agg = aggregate(report.samples, step)
        if not agg:
            print(f"{step}: no samples")
            continue
        budget = budgets.get(step, {})
        print(
            f"{step}: n={agg['n']} p50_wall={agg['p50_wall_ms']} p95_wall={agg['p95_wall_ms']} "
            f"p50_remote={agg['p50_remote_ms']} p95_remote={agg['p95_remote_ms']} "
            f"budget_p95={budget.get('p95')}"
        )
    if report.human_gate_samples:
        print("--- human gates (excluded from service budgets) ---")
        by_gate: Dict[str, List[float]] = {}
        for s in report.human_gate_samples:
            by_gate.setdefault(s.step, []).append(s.human_gate_ms)
        for gate, vals in sorted(by_gate.items()):
            print(f"{gate}: n={len(vals)} p50={round(percentile(vals, 50), 1)} p95={round(percentile(vals, 95), 1)}")


async def run_staging_iteration(report: RunReport, query: str) -> None:
    from booking_agent.backend.live import LiveUSPBackend
    from booking_agent.headless import build_search_intent, discover_services
    from booking_agent.observability import bind, emit_event, new_correlation_id, reset, set_event_sink

    cid = new_correlation_id()
    token = bind(cid)
    captured: List[Dict[str, Any]] = []

    def sink(record: Dict[str, Any]) -> None:
        captured.append(record)
        sys.stderr.write(json.dumps(record, default=str) + "\n")

    set_event_sink(sink)
    try:
        backend = LiveUSPBackend()
        intent = build_search_intent(query=query)
        try:
            await discover_services(backend, intent)
        except Exception as exc:
            emit_event(
                "soak_staging_error",
                error_type=type(exc).__name__,
                message=str(exc)[:200],
            )
    finally:
        reset(token)
    for rec in captured:
        report.add_event(rec)
    emit_event("soak_iteration_complete", mode="staging", correlation_id=cid)


async def run_hermetic_iteration(report: RunReport) -> None:
    from booking_agent.observability import bind, new_correlation_id, reset, set_event_sink
    from booking_agent.step_timing import timed_step

    cid = new_correlation_id()
    token = bind(cid)
    captured: List[Dict[str, Any]] = []

    def sink(record: Dict[str, Any]) -> None:
        captured.append(record)

    set_event_sink(sink)
    try:
        async with timed_step("discover"):
            await asyncio.sleep(0.01)
        async with timed_step("profile_resolve"):
            await asyncio.sleep(0.02)
        async with timed_step("availability"):
            await asyncio.sleep(0.01)
        async with timed_step("checkout_create"):
            await asyncio.sleep(0.01)
        async with timed_step("pay"):
            await asyncio.sleep(0.01)
        async with timed_step("confirm"):
            await asyncio.sleep(0.01)
    finally:
        reset(token)
    for rec in captured:
        report.add_event(rec)


def load_budgets(path: Optional[Path]) -> Dict[str, Dict[str, float]]:
    if path is None:
        return dict(DEFAULT_BUDGETS)
    data = json.loads(path.read_text())
    return data.get("steps", data)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="booking-agent-cli latency/soak harness")
    parser.add_argument("--mode", choices=["hermetic", "staging"], default="hermetic")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--query", default=os.environ.get("BOOKING_AGENT_STAGING_SEARCH_QUERY", "haircut"))
    parser.add_argument("--budgets", type=Path, help="JSON budgets file (ops/budgets.json)")
    parser.add_argument("--fail-on-breach", action="store_true", default=True)
    parser.add_argument("--no-fail-on-breach", dest="fail_on_breach", action="store_false")
    args = parser.parse_args(argv)

    budgets = load_budgets(args.budgets or (REPO_ROOT / "ops" / "budgets.json"))
    registry_url = os.environ.get("USP_REGISTRY_URL", DEFAULT_REGISTRY_URL).strip()
    report = RunReport(
        mode=args.mode,
        iterations=args.iterations,
        concurrency=args.concurrency,
        registry_url=registry_url,
        query=args.query,
    )

    for _ in range(args.iterations):
        if args.mode == "hermetic":
            asyncio.run(run_hermetic_iteration(report))
        else:
            asyncio.run(run_staging_iteration(report, args.query))

    print_report(report, budgets)
    breaches = check_budgets(report, budgets)
    if breaches:
        print("BUDGET BREACHES:", file=sys.stderr)
        for b in breaches:
            print(f"  - {b}", file=sys.stderr)
        return 1 if args.fail_on_breach else 0
    print("all steps within published budgets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
