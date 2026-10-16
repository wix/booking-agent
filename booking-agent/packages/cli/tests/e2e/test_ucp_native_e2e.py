"""Hermetic and optional live UCP-native E2E tests (``e2e.ucp_native``)."""
from __future__ import annotations

import asyncio

import pytest

from e2e.ucp_native import run_ucp_native_e2e


@pytest.fixture
def ucp_mock_env(monkeypatch, tmp_path):
    from support.mock.ucp_server import MockUcpServer

    server = MockUcpServer()
    server.start()
    # Keep the proof-of-possession keystore out of the developer's home.
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    # Offline-only loopback override. A conformant business rejects this RFC 6890
    # address, but the hermetic test must exercise the real HTTP fetch path.
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", server.platform_profile_url)
    monkeypatch.setenv("USP_REGISTRY_URL", "https://registry.test/search")
    yield server
    server.stop()


def test_business_use_fetches_live_catalog_before_calendar(runner, isolated_session):
    from booking_agent_cli.main import app

    runner.invoke(app, ["flow", "init", "--user-request", "Book AC repair", "--force"])
    r = runner.invoke(
        app,
        [
            "flow",
            "business",
            "use",
            "--profile-url",
            "https://kobym9.wixsite.com/hvac-koby/.well-known/usp",
            "--service-id",
            "e4d4b7a4-0e66-4104-891b-ba5bb4a04092",
        ],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    import json

    body = json.loads(r.stdout)
    assert body["data"]["live_service"]["id"] == "e4d4b7a4-0e66-4104-891b-ba5bb4a04092"


def test_ucp_native_e2e_mock(ucp_mock_env, monkeypatch):
    from support.mock import MockUSPBackend

    # Do not add isolated_session here: its profile stub replaces the loopback
    # business identity and sends checkout traffic to mock-ucp.test.
    mock_backend = MockUSPBackend()
    orig_search = mock_backend.search_registry

    async def patched_search(intent):
        services = await orig_search(intent)
        if not services:
            return services
        s = services[0]
        biz = s.business
        if not biz:
            return services
        new_biz = biz.model_copy(update={"profile_url": ucp_mock_env.profile_url})
        return [s.model_copy(update={"business": new_biz})]

    monkeypatch.setattr(mock_backend, "search_registry", patched_search)

    result = asyncio.run(
        run_ucp_native_e2e(
            registry_url="https://registry.test/search",
            query="AC repair",
            spt="spt_e2e_test",
            calendar_skip=True,
            backend=mock_backend,
        )
    )
    assert result["ok"] is True
    assert result["order_id"]
    assert result["checkout_id"]
    assert any(step["step"] == "complete_checkout" for step in result["steps"])
    assert ("POST", "/ucp/v1/checkout-sessions") in ucp_mock_env.requests
    assert (
        "POST",
        f"/ucp/v1/checkout-sessions/{result['checkout_id']}/complete",
    ) in ucp_mock_env.requests
