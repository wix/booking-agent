"""Tests for the shared USP/UCP platform identity and profile artifact."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import httpx
import pytest

from booking_agent.config import load_config
from booking_agent.platform_profile import (
    DEFAULT_PLATFORM_PROFILE_URL,
    platform_profile_doc,
    platform_profile_url,
)
from booking_agent.ucp_negotiation import intersect_ucp_capabilities, load_platform_profile
from booking_agent.rest_client import USPBusinessRestClient
from booking_agent.ucp_checkout import UcpCheckoutClient, build_paid_bookings_create_body
from booking_agent_cli.payment_helpers import complete_ucp_checkout_async

# The hosted artifact sits at the repository root, above booking-agent/.
_ARTIFACT = Path(__file__).resolve().parents[5] / "platform-profile.json"


def test_platform_profile_doc_has_ucp_version():
    doc = platform_profile_doc()
    serialized = json.dumps(doc).lower()
    assert doc["ucp"]["version"] == "2026-08-25"
    assert "2026-04-08" in doc["ucp"]["supported_versions"]
    checkout_versions = {
        entry["version"]
        for entry in doc["ucp"]["capabilities"]["dev.ucp.shopping.checkout"]
    }
    assert checkout_versions == {"2026-08-25", "2026-04-08"}
    assert "dev.ucp.shopping.order" in doc["ucp"]["capabilities"]
    terms = doc["ucp"]["capabilities"]["dev.ucp.common.payment.terms"][0]
    assert set(terms["extends"]) == {
        "dev.ucp.shopping.checkout",
        "dev.ucp.shopping.order",
    }
    assert doc["usp"]["version"] == "2026-08-14"
    assert "keys" not in doc and "signing_keys" not in doc
    for prohibited in ("mcp", "a2a", "webhook_url"):
        assert prohibited not in serialized
    services = doc["usp"]["services"]
    assert services
    assert all(
        binding["transport"] == "rest"
        for bindings in services.values()
        for binding in bindings
    )
    checkout = doc["ucp"]["capabilities"]["dev.ucp.shopping.checkout"][0]
    assert checkout["spec"] and checkout["schema"]
    assert "com.stripe.payments" in doc["ucp"]["payment_handlers"]


def test_published_platform_profile_artifact_is_rest_only_and_keyless():
    artifact = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    serialized = json.dumps(artifact).lower()
    for prohibited in ("mcp", "a2a", "webhook_url"):
        assert prohibited not in serialized
    services = artifact["usp"]["services"]
    assert all(
        binding["transport"] == "rest"
        for bindings in services.values()
        for binding in bindings
    )


def test_published_capabilities_match_client_surface():
    artifact = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    assert artifact == platform_profile_doc()

    usp_client_capabilities = {
        capability
        for capability, method in {
            "dev.usp-protocol.services.catalog": "list_services",
            "dev.usp-protocol.services.availability": "query_availability",
            "dev.usp-protocol.services.bookings": "create_booking",
        }.items()
        if callable(getattr(USPBusinessRestClient, method, None))
    }
    ucp_client_capabilities = set()
    if callable(getattr(UcpCheckoutClient, "create_checkout", None)):
        ucp_client_capabilities.add("dev.ucp.shopping.checkout")
        ucp_client_capabilities.add("dev.ucp.shopping.order")
        ucp_client_capabilities.add("dev.ucp.common.payment.terms")
    if callable(build_paid_bookings_create_body):
        ucp_client_capabilities.add("dev.usp-protocol.services.paid_bookings")
    # pay_at_service is declared only because the client can complete a checkout
    # with no payment instrument (USP 11.3.5 S4/S5).
    if "without_instrument" in inspect.signature(complete_ucp_checkout_async).parameters:
        ucp_client_capabilities.add("dev.usp-protocol.services.pay_at_service")

    assert set(artifact["usp"]["capabilities"]) == usp_client_capabilities
    assert set(artifact["ucp"]["capabilities"]) == ucp_client_capabilities


def test_platform_profile_negotiates_live_paid_bookings_version():
    business = {
        "ucp": {
            "capabilities": {
                "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}],
                "dev.usp-protocol.services.paid_bookings": [
                    {
                        "version": "2026-08-20",
                        "extends": "dev.ucp.shopping.checkout",
                    }
                ],
            }
        }
    }

    negotiated = intersect_ucp_capabilities(platform_profile_doc(), business)

    assert negotiated["dev.usp-protocol.services.paid_bookings"] == ["2026-08-20"]


def test_profile_url_defaults_to_canonical(monkeypatch):
    monkeypatch.delenv("USP_AGENT_PROFILE_URL", raising=False)
    monkeypatch.delenv("BOOKING_AGENT_UCP_PLATFORM_PROFILE_URL", raising=False)

    config = load_config()

    assert config.usp_agent_profile_url == DEFAULT_PLATFORM_PROFILE_URL
    assert platform_profile_url() == DEFAULT_PLATFORM_PROFILE_URL


def test_one_profile_override_drives_usp_and_ucp(monkeypatch):
    override = "http://127.0.0.1:8097/platform-profile.json"
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", override)
    monkeypatch.setenv(
        "BOOKING_AGENT_UCP_PLATFORM_PROFILE_URL",
        "https://ignored.example/platform-profile.json",
    )

    config = load_config()

    assert config.usp_agent_profile_url == override
    assert platform_profile_url() == override


@pytest.mark.asyncio
async def test_load_platform_profile_fetches_configured_url_over_http(monkeypatch):
    profile_url = "http://127.0.0.1:8097/platform-profile.json"
    requested = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json=platform_profile_doc())

    def fake_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("booking_agent.http_trace.traced_async_client", fake_client)

    doc = await load_platform_profile(profile_url)

    assert doc == platform_profile_doc()
    assert requested == [profile_url]


def test_platform_profile_declares_pay_at_service_per_usp_11_3_2():
    entry = platform_profile_doc()["ucp"]["capabilities"][
        "dev.usp-protocol.services.pay_at_service"
    ][0]
    assert entry["version"] == "2026-08-20"
    assert entry["extends"] == "dev.ucp.shopping.checkout"
    assert entry["schema"] == "https://usp-protocol.dev/schemas/services/pay_at_service.json"
    # 11.3.2: declaring pay_at_service without the terms extension is malformed.
    assert "dev.ucp.common.payment.terms" in platform_profile_doc()["ucp"]["capabilities"]
