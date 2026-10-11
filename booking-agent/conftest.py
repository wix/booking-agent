"""Root conftest — make workspace src/ dirs importable in-place for local runs.

Production installs use ``pip install -e ./packages/...``; this conftest only
exists so ``pytest`` works directly from the repo without a workspace sync.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent
for src in (
    _ROOT / "packages" / "usp-core" / "src",
    _ROOT / "packages" / "cli" / "src",
    _ROOT / "packages" / "cli" / "tests",  # makes `support.mock` importable in tests
):
    p = str(src)
    if src.exists() and p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _reset_observability_context():
    from booking_agent.observability import reset, reset_event_sink
    from booking_agent.step_timing import drain_pending_timings

    drain_pending_timings()
    reset()
    reset_event_sink()
    yield
    drain_pending_timings()
    reset()
    reset_event_sink()


@pytest.fixture(autouse=True)
def _stub_agent_host_ip(monkeypatch):
    """Checkout signals look up this machine's public IP; tests never leave the host."""
    import booking_agent.signals as signals

    async def _documentation_address():
        return "203.0.113.10"

    signals.reset_agent_host_ip_cache()
    monkeypatch.setattr(signals, "_fetch_public_ip", _documentation_address)
    monkeypatch.delenv(signals.RUNS_ON_BUYER_DEVICE_ENV, raising=False)
    monkeypatch.delenv(signals.BUYER_DEVICE_IP_ENV, raising=False)
    yield
    signals.reset_agent_host_ip_cache()


@pytest.fixture(autouse=True)
def _reset_http_circuits():
    """Keep per-host circuit state from leaking across tests in this process."""
    from booking_agent.circuit_breaker import reset_default_breaker

    reset_default_breaker()
    yield
    reset_default_breaker()
