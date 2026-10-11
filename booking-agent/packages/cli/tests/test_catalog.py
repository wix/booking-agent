"""Tests for live catalog fetch (USP §6.3)."""
from __future__ import annotations

from booking_agent.models.wire import (
    AvailabilityHint,
    AvailabilitySlotBitmap,
    CancellationPolicy,
    Channel,
    Duration,
    Pricing,
    ReschedulingPolicy,
    ServicePolicies,
    USPService,
    BookingWindow,
)

from booking_agent_cli.catalog import (
    authoritative_amount_minor_units_for_session,
    discover_summary_from_service,
    live_service_summary,
    payment_price_for_session,
    pricing_consistency_for_session,
    sort_discover_summaries_by_rank,
)
from booking_agent_cli.payment_helpers import resolve_spend_request_fields_sync
from booking_agent_cli.session import BookingRef, Business, Session


def test_live_service_summary_includes_authoritative_fields():
    service = USPService(
        id="svc-1",
        business_id="biz-1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT1H"),
        pricing=Pricing(model="fixed", amount=12000, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            booking_window=BookingWindow(min_advance="PT1H", max_advance="P30D"),
            confirmation_mode="auto",
            requires_payment=True,
            payment_timing="at_booking",
        ),
    )
    summary = live_service_summary(service)
    assert summary["id"] == "svc-1"
    assert summary["name"] == "AC Repair"
    assert summary["type"] == "appointment"
    assert summary["pricing"]["amount"] == 12000
    assert summary["pricing"]["currency"] == "USD"
    assert summary["pricing"]["model"] == "fixed"
    assert summary["policies"]["confirmation_mode"] == "auto"
    assert summary["policies"]["requires_payment"] is True
    assert summary["policies"]["payment_timing"] == "at_booking"
    assert summary["fetched_at"]


def test_live_service_summary_includes_availability_hint():
    service = USPService(
        id="svc-1",
        business_id="biz-1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT1H"),
        pricing=Pricing(model="fixed", amount=12000, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            booking_window=BookingWindow(min_advance="PT1H", max_advance="P30D"),
            confirmation_mode="auto",
            requires_payment=True,
            payment_timing="at_booking",
        ),
        availability_hint=AvailabilityHint(
            summary="Available tomorrow afternoon",
            generated_at="2026-07-11T12:00:00Z",
            next_available_date="2026-07-12",
        ),
    )
    summary = live_service_summary(service)
    assert summary["availability_hint"]["summary"] == "Available tomorrow afternoon"
    assert summary["availability_hint"]["next_available_date"] == "2026-07-12"


def test_live_service_summary_retains_structured_availability_hint():
    service = USPService(
        id="svc-1",
        business_id="biz-1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT1H"),
        pricing=Pricing(model="fixed", amount=12000, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            booking_window=BookingWindow(min_advance="PT1H", max_advance="P30D"),
            confirmation_mode="auto",
            requires_payment=True,
        ),
        availability_hint=AvailabilityHint(
            summary="Openings this week",
            generated_at="2026-07-11T12:00:00Z",
            valid_until="2026-07-11T12:15:00Z",
            slot_bitmaps=[
                AvailabilitySlotBitmap(
                    duration="PT60M",
                    starts_at="2026-07-12T09:00:00Z",
                    start_interval="PT30M",
                    slot_count=16,
                    encoding="roaring32-portable-base64",
                    bitmap="OjAAAAEAAAAAAAMAEAAAAAAAAQAOAA8A",
                )
            ],
        ),
    )
    hint = live_service_summary(service)["availability_hint"]
    assert hint["valid_until"] == "2026-07-11T12:15:00Z"
    assert hint["slot_bitmaps"][0]["slot_count"] == 16


def test_discover_summary_from_service_marks_registry_snapshot():
    service = USPService(
        id="svc-1",
        business_id="biz-1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT1H"),
        pricing=Pricing(model="fixed", amount=12000, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            booking_window=BookingWindow(min_advance="PT1H", max_advance="P30D"),
            confirmation_mode="auto",
            requires_payment=True,
            payment_timing="at_booking",
        ),
        metadata={"catalog_source": "registry", "profile_resolved": False},
    )
    summary = discover_summary_from_service(service)
    assert summary["service_id"] == "svc-1"
    assert summary["catalog_source"] == "registry"
    assert summary["profile_resolved"] is False
    assert "availability_hint" not in summary
    assert "rank_signals" not in summary


def test_discover_summary_exposes_rank_signals_without_reordering():
    service = USPService(
        id="svc-1",
        business_id="biz-1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT1H"),
        pricing=Pricing(model="fixed", amount=12000, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            confirmation_mode="auto",
            requires_payment=False,
        ),
        metadata={
            "search_rank": 3,
            "registry_search_result": {
                "rankSignals": {
                    "relevance": 0.7,
                    "coverage": 0,
                    "density": None,
                    "soonness": None,
                    "hintUsable": False,
                }
            },
        },
    )
    summary = discover_summary_from_service(service)
    assert summary["search_rank"] == 3
    assert summary["rank_signals"]["coverage"] == 0
    assert summary["rank_signals"]["density"] is None


def _live_service(**overrides):
    fields = dict(
        id="svc-1",
        business_id="biz-1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT1H"),
        pricing=Pricing(model="fixed", amount=10000, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            booking_window=BookingWindow(min_advance="PT1H", max_advance="P30D"),
            confirmation_mode="auto",
            requires_payment=True,
            payment_timing="at_booking",
        ),
    )
    fields.update(overrides)
    return USPService(**fields)


def test_discover_summary_shows_live_catalog_fields_and_lists_registry_mismatches():
    service = _live_service(metadata={
        "catalog_source": "live",
        "registry_search_result": {
            "serviceId": "svc-1",
            "serviceName": "AC Repair",
            "durationMinutes": 90,
            "pricing": {"amount": 100, "currency": "USD", "model": "fixed"},
        },
    })
    summary = discover_summary_from_service(service)
    assert summary["price_display"] == "$100.00"
    assert summary["price_minor_units"] == 10000
    assert summary["duration"] == "PT1H"
    assert summary["channel"] == {"type": "at_business_location"}
    assert summary["field_sources"] == {
        "price": "live_catalog",
        "duration": "live_catalog",
        "channel": "live_catalog",
        "locations": "live_catalog",
    }
    assert summary["registry_snapshot"]["price_display"] == "$1.00"
    assert summary["registry_snapshot"]["duration_minutes"] == 90
    assert {m["field"]: m for m in summary["discovery_mismatches"]} == {
        "price": {"field": "price", "registry": "$1.00", "live_catalog": "$100.00"},
        "duration": {"field": "duration", "registry": "90 minutes", "live_catalog": "60 minutes"},
    }


def test_discover_summary_without_registry_disagreement_has_no_mismatches():
    service = _live_service(metadata={
        "catalog_source": "live",
        "registry_search_result": {
            "serviceId": "svc-1",
            "serviceName": "AC Repair",
            "durationMinutes": 60,
            "pricing": {"amount": 10000, "currency": "USD", "model": "fixed"},
        },
    })
    summary = discover_summary_from_service(service)
    assert "discovery_mismatches" not in summary
    assert summary["price_display"] == "$100.00"


def test_live_free_service_shows_zero_price():
    service = _live_service(
        pricing=Pricing(model="free", currency="USD"),
        metadata={"catalog_source": "live"},
    )
    summary = discover_summary_from_service(service)
    assert summary["price_display"] == "$0.00"
    assert summary["field_sources"]["price"] == "live_catalog"


def test_registry_only_row_labels_every_field_as_registry():
    service = _live_service(metadata={
        "catalog_source": "registry",
        "profile_resolved": False,
        "registry_search_result": {
            "serviceId": "svc-1",
            "pricing": {"amount": 100, "currency": "USD", "model": "fixed"},
        },
    })
    summary = discover_summary_from_service(service)
    assert summary["price_display"] == "$1.00"
    assert set(summary["field_sources"].values()) == {"registry"}
    assert "discovery_mismatches" not in summary


def test_presented_service_changes_flags_price_channel_and_duration():
    from booking_agent_cli.catalog import presented_service_changes

    presented = {
        "service_name": "AC Repair",
        "price_minor_units": 100,
        "price_display": "$1.00",
        "duration": "PT1H",
        "channel": {"type": "at_buyer_location"},
    }
    live = live_service_summary(_live_service(duration=Duration(fixed="PT1H30M")))
    changes = {c["field"]: c for c in presented_service_changes(presented, live)}
    assert changes["price"] == {"field": "price", "presented": "$1.00", "live_catalog": "$100.00"}
    assert changes["channel"]["live_catalog"] == "at_business_location"
    assert changes["duration"]["live_catalog"] == "90 minutes"
    assert "service_name" not in changes
    assert presented_service_changes(None, live) == []


def test_sort_discover_summaries_by_rank():
    shuffled = [
        {"service_id": "svc-2", "service_name": "Second", "search_rank": 1},
        {"service_id": "svc-1", "service_name": "First", "search_rank": 0},
        {"service_id": "svc-3", "service_name": "Third", "search_rank": 2},
    ]
    ordered = sort_discover_summaries_by_rank(shuffled)
    assert [row["service_id"] for row in ordered] == ["svc-1", "svc-2", "svc-3"]


def test_discover_summary_uses_registry_pricing_display_field():
    service = USPService(
        id="svc-1",
        business_id="biz-1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT1H"),
        pricing=Pricing(model="fixed", amount=10000, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            booking_window=BookingWindow(min_advance="PT1H", max_advance="P30D"),
            confirmation_mode="auto",
            requires_payment=True,
            payment_timing="at_booking",
        ),
        metadata={
            "catalog_source": "registry",
            "registry_search_result": {
                "serviceId": "svc-1",
                "pricing": {
                    "model": "fixed",
                    "amount": 1,
                    "amountMinorUnits": 100,
                    "currency": "USD",
                    "display": "$1.00",
                },
            },
        },
    )
    summary = discover_summary_from_service(service)
    assert summary["price_display"] == "$1.00"


def test_payment_price_prefers_payment_context():
    session = Session(
        service_id="svc-1",
        discover_services=[
            {
                "service_id": "svc-1",
                "price_display": "$1.00",
                "price_minor_units": 100,
                "currency": "USD",
            }
        ],
        live_service={
            "pricing": {
                "price_display": "$100.00",
                "amount_minor_units": 10000,
                "currency": "USD",
            }
        },
        booking=BookingRef(
            payment_context={"amountDue": 5000, "currency": "USD"},
        ),
    )
    info = payment_price_for_session(session)
    assert info["price_display"] == "$50.00"
    assert info["price_source"] == "payment_context"


def test_payment_price_prefers_discover_over_live_catalog():
    session = Session(
        service_id="svc-1",
        discover_services=[
            {
                "service_id": "svc-1",
                "price_display": "$1.00",
                "price_minor_units": 100,
                "currency": "USD",
                "registry_search_result": {
                    "pricing": {
                        "amountMinorUnits": 100,
                        "currency": "USD",
                        "display": "$1.00",
                    },
                },
            }
        ],
        live_service={
            "pricing": {
                "price_display": "$100.00",
                "amount_minor_units": 10000,
                "currency": "USD",
            }
        },
    )
    info = payment_price_for_session(session)
    assert info["price_display"] == "$1.00"
    assert info["price_source"] == "discover_registry"
    assert info["amount_minor_units"] == 100


def test_authoritative_amount_minor_units_prefers_registry_over_live():
    session = Session(
        service_id="svc-1",
        checkout_mode="redirect",
        discover_services=[
            {
                "service_id": "svc-1",
                "price_display": "$1.00",
                "currency": "USD",
                "registry_search_result": {
                    "pricing": {
                        "amount": 1,
                        "amountMinorUnits": 100,
                        "currency": "USD",
                        "display": "$1.00",
                    },
                },
            }
        ],
        live_service={
            "pricing": {
                "price_display": "$100.00",
                "amount_minor_units": 10000,
                "currency": "USD",
            }
        },
        booking=BookingRef(checkout_path="ucp"),
    )
    info = authoritative_amount_minor_units_for_session(session)
    assert info["amount_minor_units"] == 100
    assert info["amount_source"] == "discover_registry"


def test_authoritative_amount_prefers_live_catalog_on_ucp_native():
    session = Session(
        service_id="svc-1",
        checkout_mode="ucp_native",
        discover_services=[
            {
                "service_id": "svc-1",
                "price_display": "$1.00",
                "currency": "USD",
                "registry_search_result": {
                    "pricing": {
                        "amountMinorUnits": 100,
                        "currency": "USD",
                        "display": "$1.00",
                    },
                },
            }
        ],
        live_service={
            "pricing": {
                "price_display": "$100.00",
                "amount_minor_units": 10000,
                "currency": "USD",
            }
        },
    )
    info = authoritative_amount_minor_units_for_session(session)
    assert info["amount_minor_units"] == 10000
    assert info["amount_source"] == "live_catalog_ucp"


def test_pricing_consistency_flags_registry_live_mismatch():
    session = Session(
        service_id="svc-1",
        checkout_mode="ucp_native",
        discover_services=[
            {
                "service_id": "svc-1",
                "price_display": "$1.00",
                "currency": "USD",
                "registry_search_result": {
                    "pricing": {"amountMinorUnits": 100, "currency": "USD"},
                },
            }
        ],
        live_service={
            "pricing": {
                "price_display": "$100.00",
                "amount_minor_units": 10000,
                "currency": "USD",
            }
        },
    )
    consistency = pricing_consistency_for_session(session)
    assert consistency["mismatch"] is True
    assert consistency["registry_amount_minor_units"] == 100
    assert consistency["live_catalog_amount_minor_units"] == 10000


def test_payment_price_uses_ucp_checkout_total_when_present():
    session = Session(
        service_id="svc-1",
        checkout_mode="ucp_native",
        ucp_checkout_amount_minor=10000,
        live_service={
            "pricing": {
                "price_display": "$100.00",
                "amount_minor_units": 10000,
                "currency": "USD",
            }
        },
    )
    info = payment_price_for_session(session)
    assert info["amount_minor_units"] == 10000
    assert info["price_source"] == "ucp_checkout_total"


def test_resolve_spend_request_fields_uses_registry_minor_units():
    session = Session(
        service_id="svc-1",
        checkout_mode="redirect",
        business=Business(name="HVAC-Koby", profile_url="https://example.test/usp"),
        discover_services=[
            {
                "service_id": "svc-1",
                "currency": "USD",
                "registry_search_result": {
                    "pricing": {"amountMinorUnits": 100, "currency": "USD"},
                },
            }
        ],
        live_service={"name": "AC Repair"},
        booking=BookingRef(checkout_path="ucp"),
    )
    fields = resolve_spend_request_fields_sync(session)
    assert fields["amount_minor_units"] == 100
    assert fields["currency"] == "USD"
    assert fields["line_item_label"] == "AC Repair"


def test_payment_price_falls_back_to_live_catalog():
    session = Session(
        service_id="svc-1",
        live_service={
            "pricing": {
                "price_display": "$1.00",
                "amount_minor_units": 100,
                "currency": "USD",
            }
        },
    )
    info = payment_price_for_session(session)
    assert info["price_display"] == "$1.00"
    assert info["price_source"] == "live_catalog"


def test_payment_price_includes_quote_caveat_before_checkout():
    session = Session(
        checkout_mode="ucp_native",
        service_id="svc-1",
        live_service={
            "pricing": {
                "price_display": "$1.00",
                "amount_minor_units": 100,
                "currency": "USD",
            }
        },
    )
    info = payment_price_for_session(session)
    assert info["quote_caveat"]
    assert info["price_source"] == "live_catalog_ucp"


def test_payment_price_uses_merchant_checkout_quote():
    session = Session(
        ucp_checkout_amount_minor=5000,
        ucp_merchant_quote={"currency": "USD", "total_minor_units": 5000},
    )
    info = payment_price_for_session(session)
    assert info["amount_minor_units"] == 5000
    assert info["currency"] == "USD"
    assert info["price_source"] == "ucp_checkout_total"
    assert info.get("quote_caveat") is None
    assert info["merchant_quote"]["total_minor_units"] == 5000

