from types import SimpleNamespace

import pytest

from booking_agent.ucp_profile import (
    UcpProfileValidationError,
    document_looks_like_ucp_native_profile,
    first_verified_ucp_native_service,
    parse_ucp_native_context_from_document,
    usp_business_profile_from_ucp_native,
)


def _minimal_valid_ucp_document() -> dict:
    return {
        "ucp": {
            "version": "2026-01-11",
            "capabilities": {
                "dev.ucp.shopping.checkout": [{"version": "2026-01-11"}],
                "dev.usp-protocol.services.paid_bookings": [
                    {"version": "1.0", "extends": "dev.ucp.shopping.checkout"}
                ],
                "dev.usp-protocol.services.catalog": [{"version": "1.0"}],
                "dev.usp-protocol.services.availability": [{"version": "1.0"}],
            },
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": "2026-01-11",
                        "transport": "rest",
                        "endpoint": "https://checkout.example.com/v1",
                    }
                ],
                "dev.usp-protocol.services": [
                    {
                        "version": "1.0",
                        "transport": "rest",
                        "endpoint": "https://usp.example.com/usp/v1",
                    }
                ],
            },
            # GH-060: payment_handlers is dict[namespace_key, list[HandlerInstance]].
            # The instance "id" (stripe_spt_demo_h1) is what goes in handler_id on
            # complete_checkout, NOT the outer namespace key (GH-061).
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {"id": "stripe_spt_demo_h1", "type": "shared_payment_token"}
                ]
            },
        },
        "business": {"name": "Demo Spa", "currency": "USD"},
    }


def test_ucp_native_context_parses_fixture():
    ctx = parse_ucp_native_context_from_document(_minimal_valid_ucp_document())
    assert ctx.paid_bookings_via_ucp_checkout is True
    assert ctx.ucp_rest_endpoint() == "https://checkout.example.com/v1"
    assert ctx.usp_rest_endpoint() == "https://usp.example.com/usp/v1"
    assert ctx.profile.business is not None
    assert ctx.profile.business.currency == "USD"
    # GH-060: payment_handlers stored as dict[namespace_key, list[HandlerInstance]]
    ns_key = "com.stripe.agentic_commerce.shared_payment_token"
    assert ns_key in ctx.profile.payment_handlers
    instances = ctx.profile.payment_handlers[ns_key]
    assert len(instances) == 1
    # GH-061: instance id is "stripe_spt_demo_h1" - this is what handler_id should equal
    assert instances[0].id == "stripe_spt_demo_h1"
    # all_payment_handler_instances() convenience accessor exposes instances for downstream
    all_instances = ctx.profile.all_payment_handler_instances()
    assert len(all_instances) == 1
    assert all_instances[0].id == "stripe_spt_demo_h1"


def test_ucp_business_timezone_propagates_to_usp_profile():
    doc = _minimal_valid_ucp_document()
    doc["business"]["timezone"] = "Asia/Jerusalem"
    ctx = parse_ucp_native_context_from_document(doc)
    assert ctx.profile.business.timezone == "Asia/Jerusalem"
    profile = usp_business_profile_from_ucp_native(ctx)
    assert profile.business.timezone == "Asia/Jerusalem"
    assert profile.business.currency == "USD"


def test_ucp_business_without_timezone_yields_none():
    ctx = parse_ucp_native_context_from_document(_minimal_valid_ucp_document())
    assert ctx.profile.business.timezone is None
    assert usp_business_profile_from_ucp_native(ctx).business.timezone is None


def test_ucp_native_context_dict_payment_handlers():
    doc = _minimal_valid_ucp_document()
    doc["ucp"]["payment_handlers"] = {
        "com.stripe.agentic_commerce.shared_payment_token": [
            {"id": "stripe_spt_demo_h1", "type": "shared_payment_token", "version": "1"}
        ]
    }
    ctx = parse_ucp_native_context_from_document(doc)
    # GH-060: dict shape preserved in model
    assert isinstance(ctx.profile.payment_handlers, dict)
    ns_key = "com.stripe.agentic_commerce.shared_payment_token"
    assert ns_key in ctx.profile.payment_handlers
    # GH-061: instance id exposed, not the namespace key
    assert ctx.profile.payment_handlers[ns_key][0].id == "stripe_spt_demo_h1"


def test_ucp_native_context_rejects_wrong_extends():
    doc = _minimal_valid_ucp_document()
    doc["ucp"]["capabilities"]["dev.usp-protocol.services.paid_bookings"][0][
        "extends"
    ] = "dev.ucp.shopping.order"
    with pytest.raises(UcpProfileValidationError, match="extends"):
        parse_ucp_native_context_from_document(doc)


def test_ucp_native_context_rejects_missing_checkout_capability():
    doc = _minimal_valid_ucp_document()
    del doc["ucp"]["capabilities"]["dev.ucp.shopping.checkout"]
    with pytest.raises(UcpProfileValidationError, match="dev.ucp.shopping.checkout"):
        parse_ucp_native_context_from_document(doc)


def _usp_capable_ucp_without_paid_bookings() -> dict:
    return {
        "ucp": {
            "version": "2026-04-08",
            "capabilities": {
                "dev.usp-protocol.services.catalog": [{"version": "2026-08-20"}],
                "dev.usp-protocol.services.availability": [{"version": "2026-08-20"}],
                "dev.usp-protocol.services.bookings": [{"version": "2026-08-20"}],
            },
            "services": {
                "dev.usp-protocol.services": [
                    {
                        "version": "2026-08-20",
                        "transport": "rest",
                        "endpoint": "https://free.example.com/_api/usp-impl/v1",
                    }
                ],
            },
        }
    }


def _generic_ucp_only_document() -> dict:
    return {
        "ucp": {
            "version": "2026-04-08",
            "capabilities": {
                "dev.ucp.shopping.checkout": [
                    {
                        "version": "2026-04-08",
                        "spec": "https://ucp.dev/specification/checkout",
                        "schema": "https://ucp.dev/schemas/shopping/checkout.json",
                    }
                ]
            },
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": "2026-04-08",
                        "transport": "rest",
                        "endpoint": "https://shop.example.com/ecom/ucp",
                    }
                ]
            },
        }
    }


def test_ucp_document_without_paid_bookings_is_still_a_ucp_profile():
    assert document_looks_like_ucp_native_profile(
        _usp_capable_ucp_without_paid_bookings()
    )
    assert document_looks_like_ucp_native_profile(_generic_ucp_only_document())


def test_parse_usp_capable_ucp_without_paid_bookings():
    ctx = parse_ucp_native_context_from_document(
        _usp_capable_ucp_without_paid_bookings(),
        require_paid_bookings=False,
    )
    assert ctx.paid_bookings_via_ucp_checkout is False
    assert ctx.usp_rest_endpoint() == "https://free.example.com/_api/usp-impl/v1"
    profile = usp_business_profile_from_ucp_native(ctx)
    assert profile.rest_endpoint == "https://free.example.com/_api/usp-impl/v1"
    assert "dev.usp-protocol.services.catalog" in profile.capabilities


def test_parse_usp_capable_ucp_accepts_capability_extends_list():
    doc = _usp_capable_ucp_without_paid_bookings()
    doc["ucp"]["capabilities"]["dev.ucp.shopping.fulfillment"] = [
        {
            "version": "2026-04-08",
            "extends": [
                "dev.ucp.shopping.checkout",
                "dev.ucp.shopping.order",
            ],
        }
    ]

    ctx = parse_ucp_native_context_from_document(
        doc,
        require_paid_bookings=False,
    )

    entry = ctx.profile.capabilities["dev.ucp.shopping.fulfillment"][0]
    assert entry.extends == [
        "dev.ucp.shopping.checkout",
        "dev.ucp.shopping.order",
    ]


def test_parse_generic_ucp_only_rejects_without_standalone_fallback():
    with pytest.raises(UcpProfileValidationError, match="USP"):
        parse_ucp_native_context_from_document(
            _generic_ucp_only_document(),
            require_paid_bookings=False,
        )


@pytest.mark.asyncio
async def test_fetch_generic_ucp_profile_raises_validation_error(monkeypatch):
    import httpx

    from booking_agent.ucp_profile import fetch_and_resolve_business_profile

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_generic_ucp_only_document())

    def fake_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr("booking_agent.http_trace.traced_async_client", fake_client)

    with pytest.raises(UcpProfileValidationError, match="USP"):
        await fetch_and_resolve_business_profile(
            "https://shop.example.com/.well-known/ucp",
            use_cache=False,
        )


def test_first_verified_ucp_native_service_skips_generic_registry_rows():
    generic = SimpleNamespace(
        id="svc-generic",
        business=SimpleNamespace(
            profile_url="https://generic.example.com/.well-known/ucp",
        ),
    )
    capable = SimpleNamespace(
        id="svc-usp",
        business=SimpleNamespace(
            profile_url="https://usp.example.com/.well-known/ucp",
        ),
    )
    selected = first_verified_ucp_native_service(
        [generic, capable],
        {"https://usp.example.com/.well-known/ucp": object()},
    )
    assert selected is capable
