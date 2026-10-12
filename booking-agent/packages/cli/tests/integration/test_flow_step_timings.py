"""Flow step timings on normal mocked commands (#161 Track A)."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from booking_agent.observability import reset_event_sink, set_event_sink
from booking_agent_cli.main import app


@pytest.fixture(autouse=True)
def _capture_events():
    captured: list = []
    set_event_sink(lambda r: captured.append(r))
    yield captured
    reset_event_sink()


def test_discover_emits_step_timing_with_correlation(isolated_session, monkeypatch, _capture_events):
    from booking_agent_cli.commands import flow as flow_mod
    from support.fixtures import MOCK_SERVICES

    svc = MOCK_SERVICES[0]
    monkeypatch.setattr(flow_mod, "resolve_backend", lambda: object())

    async def fake_discover(backend, intent):
        from booking_agent.step_timing import timed_step

        async with timed_step("discover"):
            pass
        async with timed_step("profile_resolve"):
            pass
        return [svc]

    monkeypatch.setattr("booking_agent.headless.discover_services", fake_discover)

    runner = CliRunner()
    r0 = runner.invoke(
        app,
        ["flow", "init", "--user-request", "haircut", "--force"],
        catch_exceptions=False,
    )
    assert r0.exit_code == 0
    r = runner.invoke(
        app,
        ["flow", "discover", "--query", "haircut"],
        catch_exceptions=False,
    )
    assert r.exit_code == 0
    body = json.loads(r.stdout)
    assert body.get("correlation_id")
    assert "discover" in body.get("data", {}).get("flow_timings", {})

    step_events = [e for e in _capture_events if e.get("event") == "step_timing"]
    assert step_events
    assert all(e.get("correlation_id") for e in step_events)
