"""A registry that rejects the search must not look like a search with no results."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from booking_agent_cli.main import app


def test_discover_reports_registry_rejection(isolated_session, monkeypatch):
    from booking_agent.rest_client import RegistrySearchRejectedError
    from booking_agent_cli.commands import flow as flow_mod

    monkeypatch.setattr(flow_mod, "resolve_backend", lambda: object())

    async def rejecting_discover(backend, intent):
        raise RegistrySearchRejectedError(
            code="validation_error",
            message="price_range requires a currency.",
            http_status=200,
            path="$.price_range",
        )

    monkeypatch.setattr("booking_agent.headless.discover_services", rejecting_discover)

    runner = CliRunner()
    r0 = runner.invoke(
        app, ["flow", "init", "--user-request", "haircut", "--force"],
        catch_exceptions=False,
    )
    assert r0.exit_code == 0
    r = runner.invoke(app, ["flow", "discover"], catch_exceptions=False)

    assert r.exit_code == 1
    data = json.loads(r.stdout)["data"]
    assert data["error"]["code"] == "registry_search_rejected"
    assert data["registry_error"] == {
        "code": "validation_error",
        "message": "price_range requires a currency.",
        "http_status": 200,
        "path": "$.price_range",
    }
