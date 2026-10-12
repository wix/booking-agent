"""Hard cutover to ``dev.usp-protocol.*`` / ``https://usp-protocol.dev`` (issue 156 AC-L)."""
from __future__ import annotations

import pathlib

import pytest

from booking_agent.discovery import USPProfileResolver
from booking_agent.namespace import (
    AUTHORITY_ORIGIN,
    CAP_CATALOG,
    CAP_PAID_BOOKINGS,
    NamespaceOriginError,
    check_origin_binding,
)
from booking_agent.platform_profile import platform_profile_doc
from booking_agent.ucp_profile import parse_ucp_native_context_from_document

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SCAN_ROOTS = (
    REPO_ROOT / "packages",
    REPO_ROOT / "skills",
    REPO_ROOT / "platform-profile.json",
)


def test_capability_constants_use_new_namespace():
    assert CAP_PAID_BOOKINGS == "dev.usp-protocol.services.paid_bookings"
    assert CAP_CATALOG == "dev.usp-protocol.services.catalog"


def test_origin_binding_rejects_legacy_usp_dev():
    legacy = "https://" + "usp.dev"
    with pytest.raises(NamespaceOriginError, match="usp-protocol.dev"):
        check_origin_binding(
            CAP_CATALOG,
            spec=f"{legacy}/specification#3-service-catalog",
            schema=f"{legacy}/schemas/services/catalog.json",
        )


def test_origin_binding_accepts_authority_origin():
    check_origin_binding(
        CAP_CATALOG,
        spec=f"{AUTHORITY_ORIGIN}/specification#3-service-catalog",
        schema=f"{AUTHORITY_ORIGIN}/schemas/services/catalog.json",
    )


def test_origin_binding_skips_missing_urls():
    check_origin_binding(CAP_CATALOG)


def test_standalone_profile_rejects_legacy_spec_origin():
    data = {
        "usp": {
            "version": "2026-08-14",
            "services": {
                "dev.usp-protocol.services": [
                    {
                        "version": "2026-08-14",
                        "transport": "rest",
                        "endpoint": "https://biz.example.com/usp/v1",
                        "spec": "https://" + "usp.dev/specification",
                    }
                ]
            },
            "capabilities": {
                CAP_CATALOG: [{"version": "2026-08-14"}],
            },
            "business": {"name": "Test Biz", "timezone": "UTC", "currency": "USD"},
        }
    }
    with pytest.raises(NamespaceOriginError):
        USPProfileResolver._parse_profile(data)


def test_ucp_profile_rejects_legacy_paid_bookings_schema_origin():
    doc = {
        "ucp": {
            "version": "2026-01-11",
            "capabilities": {
                "dev.ucp.shopping.checkout": [{"version": "2026-01-11"}],
                CAP_PAID_BOOKINGS: [
                    {
                        "version": "1.0",
                        "extends": "dev.ucp.shopping.checkout",
                        "schema": "https://" + "usp.dev/schemas/services/paid_bookings.json",
                    }
                ],
            },
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": "2026-01-11",
                        "transport": "rest",
                        "endpoint": "https://checkout.example.com/v1",
                    }
                ],
            },
        },
        "business": {"name": "Demo Spa", "currency": "USD"},
    }
    with pytest.raises(NamespaceOriginError):
        parse_ucp_native_context_from_document(doc)


def test_platform_profile_advertises_new_authority():
    doc = platform_profile_doc()
    assert CAP_CATALOG in doc["usp"]["capabilities"]
    assert CAP_PAID_BOOKINGS in doc["ucp"]["capabilities"]
    schema = doc["usp"]["capabilities"][CAP_CATALOG][0]["schema"]
    assert schema.startswith(AUTHORITY_ORIGIN)


def test_source_tree_has_no_legacy_namespace_literals():
    hits: list[str] = []
    paths: list[pathlib.Path] = []
    for root in SCAN_ROOTS:
        if root.is_file():
            paths.append(root)
        else:
            paths.extend(
                p
                for p in root.rglob("*")
                if p.is_file() and p.suffix in {".py", ".json", ".md", ".html", ".toml"}
            )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT)
        if ("https://" + "usp.dev") in text:
            hits.append(f"{rel}: legacy authority URL")
        if ("dev.usp" + ".") in text:
            hits.append(f"{rel}: legacy capability prefix")
    assert hits == []
