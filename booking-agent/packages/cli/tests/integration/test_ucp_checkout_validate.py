import httpx
import pytest

from booking_agent.models.wire import (
    CancellationPolicy,
    Channel,
    Duration,
    Pricing,
    PricingModel,
    ReschedulingPolicy,
    ServicePolicies,
    USPService,
)
from booking_agent.ucp_checkout import (
    MandatoryAcceptancePending,
    build_paid_bookings_create_body,
    build_payment_instrument_for_complete_checkout,
    checkout_line_item_price_minor_units,
    checkout_subtotal_minor_units,
    checkout_tax_minor_units,
    checkout_total_minor_units,
    extract_merchant_quote,
    extract_spt_handler_config_from_checkout,
    handler_id_from_checkout_payment,
    payment_handler_ids_from_checkout_document,
    payment_handler_ids_from_ucp_profile_document,
    resolve_handler_id_for_ucp_complete,
    validate_completed_checkout_selected_term,
    validate_payment_terms,
    validate_ucp_checkout_canceled,
    validate_checkout_totals_and_links,
    validate_create_checkout_ready,
    validate_ucp_checkout_completed,
)
from booking_agent.ucp_profile import UcpProfileValidationError


def test_totals_links_required_when_ready():
    with pytest.raises(UcpProfileValidationError, match="totals"):
        validate_checkout_totals_and_links({"status": "ready_for_complete"})

    with pytest.raises(UcpProfileValidationError, match="totals"):
        validate_checkout_totals_and_links(
            {"status": "ready_for_complete", "totals": {"line_items": []}}
        )

    with pytest.raises(UcpProfileValidationError, match="links"):
        validate_checkout_totals_and_links(
            {
                "status": "ready_for_complete",
                "totals": [{"type": "total", "amount": 1000}],
            }
        )

    with pytest.raises(UcpProfileValidationError, match="links"):
        validate_checkout_totals_and_links(
            {
                "status": "completed",
                "totals": [{"type": "total", "amount": 1000}],
                "links": "https://example.com/terms",
            }
        )

    validate_checkout_totals_and_links(
        {
            "status": "ready_for_complete",
            "totals": [{"type": "total", "amount": 1000}],
            "links": [],
        }
    )

    validate_checkout_totals_and_links(
        {
            "status": "ready_for_complete",
            "totals": [
                {"type": "subtotal", "amount": 1000},
                {"type": "total", "amount": 1000},
            ],
            "links": [{"type": "terms", "url": "https://example.com/t"}],
        }
    )


def test_checkout_total_minor_units_reads_total_entry():
    assert checkout_total_minor_units(
        {
            "totals": [
                {"type": "subtotal", "amount": 10000},
                {"type": "total", "amount": 10000},
            ]
        }
    ) == 10000
    assert checkout_total_minor_units({"totals": []}) is None


def test_checkout_total_minor_units_does_not_fallback_to_subtotal():
    assert checkout_total_minor_units(
        {"totals": [{"type": "subtotal", "amount": 10000}]}
    ) is None


def test_extract_merchant_quote_copies_total_tax_currency():
    quote = extract_merchant_quote(
        {
            "currency": "USD",
            "totals": [
                {"type": "subtotal", "amount": 9000},
                {"type": "tax", "amount": 1000},
                {"type": "total", "amount": 10000},
            ],
        }
    )
    assert quote == {
        "currency": "USD",
        "total_minor_units": 10000,
        "tax_minor_units": 1000,
        "subtotal_minor_units": 9000,
    }


_WIX_TAXED_CHECKOUT = {
    "currency": "USD",
    "line_items": [
        {"id": "li_1", "quantity": 1, "item": {"id": "svc", "title": "Home HVAC Repair", "price": 100}},
    ],
    "totals": [
        {"type": "subtotal", "amount": 100},
        {"type": "tax", "amount": 10},
        {"type": "total", "amount": 110},
    ],
}


def test_line_item_price_reads_the_booked_item_not_the_total():
    assert checkout_line_item_price_minor_units(_WIX_TAXED_CHECKOUT) == 100
    assert checkout_subtotal_minor_units(_WIX_TAXED_CHECKOUT) == 100
    assert extract_merchant_quote(_WIX_TAXED_CHECKOUT) == {
        "currency": "USD",
        "total_minor_units": 110,
        "tax_minor_units": 10,
        "subtotal_minor_units": 100,
        "line_item_price_minor_units": 100,
    }


def test_line_item_price_falls_back_to_the_only_item_and_is_none_when_ambiguous():
    single = {"line_items": [{"id": "merchant_li", "item": {"price": 250}}]}
    assert checkout_line_item_price_minor_units(single) == 250
    many = {"line_items": [{"id": "a", "item": {"price": 1}}, {"id": "b", "item": {"price": 2}}]}
    assert checkout_line_item_price_minor_units(many) is None
    by_id = {"line_items": [{"id": "a", "item": {"price": 1}}, {"id": "li_1", "item": {"price": 2}}]}
    assert checkout_line_item_price_minor_units(by_id) == 2
    assert checkout_line_item_price_minor_units({}) is None
    assert checkout_line_item_price_minor_units({"line_items": [{"id": "li_1", "item": {}}]}) is None
    assert checkout_subtotal_minor_units({"totals": [{"type": "total", "amount": 5}]}) is None


def test_extract_merchant_quote_omits_tax_when_absent():
    quote = extract_merchant_quote(
        {
            "currency": "EUR",
            "totals": [{"type": "total", "amount": 5000}],
        }
    )
    assert quote == {"currency": "EUR", "total_minor_units": 5000}
    assert "tax_minor_units" not in quote


def test_payment_handler_ids_from_profile_dict_shape():
    doc = {
        "ucp": {
            "payment_handlers": {
                "com.stripe": [
                    {"id": "stripe_spt_main", "version": "1"},
                    {"id": "stripe_backup", "version": "1"},
                ]
            }
        }
    }
    assert payment_handler_ids_from_ucp_profile_document(doc) == ["stripe_spt_main", "stripe_backup"]


def test_resolve_from_checkout_ucp_dict_single_handler():
    ch = {
        "ucp": {
            "payment_handlers": {
                "com.stripe": [{"id": "h_demo", "available_instruments": [{"type": "shared_payment_token"}]}]
            }
        }
    }
    assert (
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None, checkout=ch, business_profile_doc={"ucp": {}},
        )
        == "h_demo"
    )


def test_build_instrument_merges_template_and_available_instrument_type():
    ch = {
        "ucp": {
            "payment_handlers": {
                "com.stripe": [{"id": "hid", "available_instruments": [{"type": "shared_payment_token"}]}]
            }
        },
        "payment": {
            "instruments": [
                {"id": "instr_1", "handler_id": "hid", "type": "shared_payment_token", "selected": True}
            ]
        },
    }
    inst = build_payment_instrument_for_complete_checkout(ch, "hid", "spt_secret")
    assert inst["handler_id"] == "hid"
    assert inst["credential"]["token"] == "spt_secret"
    assert inst.get("type") == "shared_payment_token"
    assert inst.get("id") == "instr_1"


def test_handler_from_checkout_prefers_selected():
    ch = {
        "payment": {
            "instruments": [
                {"handler_id": "h1", "selected": False},
                {"handlerId": "h2", "selected": True},
            ]
        }
    }
    assert handler_id_from_checkout_payment(ch) == "h2"


def test_resolve_handler_explicit_overrides():
    ch: dict = {}
    prof = {"ucp": {"payment_handlers": [{"id": "a"}, {"id": "b"}]}}
    assert (
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id="z", checkout=ch, business_profile_doc=prof,
        )
        == "z"
    )


def test_resolve_handler_single_profile_entry():
    # GH-060/GH-061: profile uses dict[namespace_key, list[HandlerInstance]]; the resolved
    # value is the instance id (stripe_spt_demo_h1), not the namespace key.
    ch: dict = {}
    prof = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {"id": "stripe_spt_demo_h1", "type": "shared_payment_token"}
                ]
            }
        }
    }
    assert (
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None, checkout=ch, business_profile_doc=prof,
        )
        == "stripe_spt_demo_h1"
    )


def test_resolve_handler_from_checkout_when_unambiguous():
    ch = {"payment": {"instruments": [{"handler_id": "chk", "selected": True}]}}
    prof = {"ucp": {"payment_handlers": [{"id": "other"}]}}
    assert (
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None, checkout=ch, business_profile_doc=prof,
        )
        == "chk"
    )


def test_resolve_handler_ambiguous_raises():
    ch: dict = {}
    prof = {"ucp": {"payment_handlers": [{"id": "a"}, {"id": "b"}]}}
    with pytest.raises(UcpProfileValidationError, match="Could not pick"):
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None, checkout=ch, business_profile_doc=prof,
        )


def test_resolve_handler_missing_everywhere_raises():
    with pytest.raises(UcpProfileValidationError, match="No handler_id"):
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None, checkout={}, business_profile_doc={"ucp": {}},
        )


def test_payment_handler_ids_from_checkout_top_level_dict():
    ch = {"payment_handlers": {"com.foo": [{"id": "only_one"}]}}
    assert payment_handler_ids_from_checkout_document(ch) == ["only_one"]


def test_checkout_handlers_preferred_over_ambiguous_profile():
    ch = {"ucp": {"payment_handlers": {"com.stripe": [{"id": "from_checkout"}]}}}
    prof = {"ucp": {"payment_handlers": [{"id": "a"}, {"id": "b"}]}}
    assert (
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None, checkout=ch, business_profile_doc=prof,
        )
        == "from_checkout"
    )


# ---------------------------------------------------------------------------
# GH-061: handler_id semantics - instance id, not namespace key
# ---------------------------------------------------------------------------

def test_handler_id_is_instance_id_not_namespace_key():
    """GH-061: payment.instruments[].handler_id must equal the instance id, not the namespace."""
    checkout = {
        "payment": {
            "instruments": [
                {
                    "handler_id": "stripe_spt_demo_h1",
                    "type": "shared_payment_token",
                    "selected": True,
                }
            ]
        }
    }
    resolved = handler_id_from_checkout_payment(checkout)
    assert resolved == "stripe_spt_demo_h1"
    # The namespace key must NOT be returned
    assert resolved != "com.stripe.agentic_commerce.shared_payment_token"


def test_build_instrument_handler_id_uses_instance_id():
    """GH-061: complete_checkout instrument carries instance id as handler_id."""
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.payments": [
                    {
                        "id": "stripe_payments",
                        "available_instruments": [{"type": "link"}],
                    }
                ]
            }
        },
        "payment": {
            "instruments": [
                {
                    "id": "instr_abc",
                    "handler_id": "stripe_payments",
                    "type": "link",
                    "selected": True,
                }
            ]
        },
    }
    inst = build_payment_instrument_for_complete_checkout(checkout, "stripe_payments", "spt_tok")
    assert inst["handler_id"] == "stripe_payments"
    assert inst["handler_id"] != "com.stripe.payments"
    assert inst["credential"]["token"] == "spt_tok"
    assert inst["credential"]["type"] == "stripe_payment_token"
    assert inst.get("type") == "link"
    assert inst.get("id") == "instr_abc"


# ---------------------------------------------------------------------------
# GH-064: SPT acquisition flow - read from checkout, not profile
# ---------------------------------------------------------------------------

def test_handler_id_resolved_from_checkout_instance_id():
    """GH-064: handler_id comes from checkout payment_handlers instance id, not namespace."""
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {
                        "id": "stripe_spt_demo_h1",
                        "available_instruments": [{"type": "shared_payment_token"}],
                    }
                ]
            }
        }
    }
    ids = payment_handler_ids_from_checkout_document(checkout)
    assert ids == ["stripe_spt_demo_h1"]
    assert "com.stripe.agentic_commerce.shared_payment_token" not in ids


def test_checkout_available_instruments_authoritative_over_profile():
    """GH-064: available_instruments from checkout overrides profile for type resolution."""
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {
                        "id": "stripe_spt_demo_h1",
                        "available_instruments": [{"type": "shared_payment_token"}],
                    }
                ]
            }
        }
    }
    from booking_agent.ucp_checkout import instrument_type_hint_from_checkout_handlers

    hint = instrument_type_hint_from_checkout_handlers(checkout, "stripe_spt_demo_h1")
    assert hint == "shared_payment_token"


def test_resolve_handler_id_from_checkout_canonical_shape():
    """GH-064: full resolution path using canonical checkout dict[ns, list[instance]] shape."""
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {
                        "id": "stripe_spt_demo_h1",
                        "available_instruments": [{"type": "shared_payment_token"}],
                    }
                ]
            }
        }
    }
    prof: dict = {"ucp": {}}
    resolved = resolve_handler_id_for_ucp_complete(
        explicit_handler_id=None, checkout=checkout, business_profile_doc=prof,
    )
    assert resolved == "stripe_spt_demo_h1"


def test_extract_spt_handler_config_network_id_from_link_instrument():
    """Primary path: network_id on Link instrument config (com.stripe.payments)."""
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.payments": [
                    {
                        "id": "stripe_payments",
                        "available_instruments": [
                            {
                                "type": "link",
                                "config": {"network_id": "ntwrk_link_primary"},
                            }
                        ],
                    }
                ]
            }
        }
    }
    cfg = extract_spt_handler_config_from_checkout(checkout)
    assert cfg["network_id"] == "ntwrk_link_primary"


def test_extract_spt_handler_config_network_id_link_instrument_camel_case():
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.payments": [
                    {
                        "id": "stripe_payments",
                        "available_instruments": [
                            {
                                "type": "link",
                                "config": {"networkId": "ntwrk_link_camel"},
                            }
                        ],
                    }
                ]
            }
        }
    }
    cfg = extract_spt_handler_config_from_checkout(checkout)
    assert cfg["network_id"] == "ntwrk_link_camel"


def _published_stripe_and_google_pay_checkout() -> dict:
    """Checkout that lists Link Agent Wallet and a second handler (Google Pay)."""
    return {
        "ucp": {
            "payment_handlers": {
                "com.google.pay": [
                    {
                        "id": "google_pay",
                        "version": "2026-01-11",
                        "config": {"network_id": "ntwrk_google_pay_decoy"},
                        "available_instruments": [{"type": "card"}],
                    }
                ],
                "com.stripe.payments": [
                    {
                        "id": "stripe_payments",
                        "version": "2026-06-25",
                        "spec": (
                            "https://docs.stripe.com/agentic-commerce/"
                            "ucp/stripe-payments-handler"
                        ),
                        "schema": "https://ucp.stripe.com/payments/2026-06-25/schema.json",
                        "config": {"network_id": "ntwrk_handler_level_legacy"},
                        "available_instruments": [
                            {
                                "type": "link",
                                "config": {"network_id": "ntwrk_link_instrument"},
                            }
                        ],
                    }
                ],
                "com.stripe": [
                    {
                        "id": "com.stripe",
                        "version": "2026-04-08",
                        "config": {"network_id": "ntwrk_old_handler_config"},
                        "available_instruments": [{"type": "link"}],
                    }
                ],
            }
        }
    }


def test_multi_handler_checkout_reads_link_instrument_network_id():
    """Production path: Link config.network_id wins over other handlers and handler config."""
    checkout = _published_stripe_and_google_pay_checkout()
    cfg = extract_spt_handler_config_from_checkout(checkout)
    assert cfg == {"network_id": "ntwrk_link_instrument"}


def test_multi_handler_checkout_completes_with_stripe_payments():
    """complete_checkout selects stripe_payments when Google Pay is also listed."""
    checkout = _published_stripe_and_google_pay_checkout()
    resolved = resolve_handler_id_for_ucp_complete(
        explicit_handler_id=None,
        checkout=checkout,
        business_profile_doc={"ucp": {}},
    )
    assert resolved == "stripe_payments"
    inst = build_payment_instrument_for_complete_checkout(
        checkout, resolved, "spt_tok",
    )
    assert inst["handler_id"] == "stripe_payments"
    assert inst["type"] == "link"
    assert inst["credential"] == {
        "type": "stripe_payment_token",
        "token": "spt_tok",
    }


def test_selected_checkout_instrument_overrides_stripe_payments_preference():
    checkout = _published_stripe_and_google_pay_checkout()
    checkout["payment"] = {
        "instruments": [
            {"handler_id": "stripe_payments", "selected": False},
            {"handler_id": "google_pay", "selected": True},
        ]
    }
    assert (
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None,
            checkout=checkout,
            business_profile_doc={"ucp": {}},
        )
        == "google_pay"
    )


def test_legacy_hvac_handler_config_network_id_is_single_handler_fallback():
    """Old HVAC shape is a fallback only when it is the sole handler."""
    legacy_only = {
        "ucp": {
            "payment_handlers": {
                "com.stripe": [
                    {
                        "id": "com.stripe",
                        "version": "2026-04-08",
                        "config": {"network_id": "ntwrk_legacy_handler"},
                        "available_instruments": [{"type": "link"}],
                    }
                ]
            }
        }
    }
    assert extract_spt_handler_config_from_checkout(legacy_only) == {
        "network_id": "ntwrk_legacy_handler",
    }

    legacy_plus_other = {
        "ucp": {
            "payment_handlers": {
                "com.stripe": [
                    {
                        "id": "com.stripe",
                        "version": "2026-04-08",
                        "config": {"network_id": "ntwrk_legacy_handler"},
                        "available_instruments": [{"type": "link"}],
                    }
                ],
                "com.google.pay": [
                    {
                        "id": "google_pay",
                        "available_instruments": [{"type": "card"}],
                    }
                ],
            }
        }
    }
    assert extract_spt_handler_config_from_checkout(legacy_plus_other) == {}
    with pytest.raises(UcpProfileValidationError, match="Could not pick"):
        resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None,
            checkout=legacy_plus_other,
            business_profile_doc={"ucp": {}},
        )


def test_extract_spt_handler_config_network_id_from_checkout():
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {
                        "id": "stripe_spt_demo_h1",
                        "config": {"network_id": "ntwrk_test_abc"},
                        "available_instruments": [{"type": "shared_payment_token"}],
                    }
                ]
            }
        }
    }
    cfg = extract_spt_handler_config_from_checkout(checkout)
    assert cfg["network_id"] == "ntwrk_test_abc"


def test_extract_spt_handler_config_network_id_camel_case():
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {
                        "id": "stripe_spt_demo_h1",
                        "config": {"networkId": "ntwrk_camel"},
                        "available_instruments": [{"type": "shared_payment_token"}],
                    }
                ]
            }
        }
    }
    cfg = extract_spt_handler_config_from_checkout(checkout)
    assert cfg["network_id"] == "ntwrk_camel"


def test_extract_spt_handler_config_by_handler_id():
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {
                        "id": "h_one",
                        "config": {"network_id": "ntwrk_one"},
                        "available_instruments": [{"type": "shared_payment_token"}],
                    },
                    {
                        "id": "h_two",
                        "config": {"network_id": "ntwrk_two"},
                        "available_instruments": [{"type": "shared_payment_token"}],
                    },
                ]
            }
        }
    }
    cfg = extract_spt_handler_config_from_checkout(checkout, handler_id="h_two")
    assert cfg["network_id"] == "ntwrk_two"


def test_extract_spt_handler_config_missing_returns_empty():
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {
                        "id": "stripe_spt_demo_h1",
                        "available_instruments": [{"type": "shared_payment_token"}],
                    }
                ]
            }
        }
    }
    assert extract_spt_handler_config_from_checkout(checkout) == {}


def test_extract_spt_handler_config_ambiguous_handlers_returns_empty():
    checkout = {
        "ucp": {
            "payment_handlers": {
                "com.stripe.agentic_commerce.shared_payment_token": [
                    {
                        "id": "h_one",
                        "config": {"network_id": "ntwrk_one"},
                        "available_instruments": [{"type": "shared_payment_token"}],
                    },
                    {
                        "id": "h_two",
                        "config": {"network_id": "ntwrk_two"},
                        "available_instruments": [{"type": "shared_payment_token"}],
                    },
                ]
            }
        }
    }
    assert extract_spt_handler_config_from_checkout(checkout) == {}


@pytest.mark.asyncio
async def test_complete_checkout_validates_totals_and_links_on_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GH-060: completed checkout responses must include totals and links."""
    from booking_agent.ucp_checkout import UcpCheckoutClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "completed"})

    def fake_traced(**kwargs):
        merged = dict(kwargs)
        merged["transport"] = httpx.MockTransport(handler)
        from booking_agent.http_trace import traced_async_client as real
        return real(**merged)

    monkeypatch.setattr("booking_agent.ucp_checkout.traced_async_client", fake_traced)

    client = UcpCheckoutClient("https://merchant.example/ucp/v1")
    with pytest.raises(UcpProfileValidationError, match="totals"):
        await client.complete_checkout("cs_test", {"payment": {"instruments": []}})


def _sample_service(*, model: str = "fixed", amount: int = 5000) -> USPService:
    return USPService(
        id="svc_1",
        business_id="biz_1",
        name="AC Repair",
        type="appointment",
        duration=Duration(fixed="PT60M"),
        pricing=Pricing(model=model, amount=amount, currency="USD"),
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            confirmation_mode="auto",
            requires_payment=True,
        ),
    )


def test_build_paid_bookings_create_body_spec_shape():
    body = build_paid_bookings_create_body(
        service=_sample_service(),
        slot_id="slot_1",
        slot_start="2026-06-25T14:00:00+00:00",
        slot_end="2026-06-25T15:00:00+00:00",
        slot_duration="PT60M",
        buyer_first="Ada",
        buyer_last="Lovelace",
        buyer_email="ada@booking-agent.test",
        currency="USD",
    )
    assert body["buyer"]["email"] == "ada@booking-agent.test"
    assert body["booking"]["slot"]["duration"] == "PT60M"
    assert body["line_items"][0]["id"] == "li_1"
    assert "buyer" not in body["booking"]


def test_build_paid_bookings_variable_pricing_uses_slot_amount():
    body = build_paid_bookings_create_body(
        service=_sample_service(model="variable", amount=9999),
        slot_id="slot_1",
        slot_start="2026-06-25T14:00:00+00:00",
        slot_end="2026-06-25T15:00:00+00:00",
        slot_duration="PT60M",
        buyer_first="Ada",
        buyer_last="Lovelace",
        buyer_email="ada@booking-agent.test",
        currency="USD",
        slot_price_amount=7500,
    )
    assert body["line_items"][0]["item"]["price"] == 7500


def test_build_paid_bookings_carries_recipient_and_buyer_extras_only_when_set():
    common = dict(
        service=_sample_service(),
        slot_id="slot_1",
        slot_start="2026-06-25T14:00:00+00:00",
        slot_end="2026-06-25T15:00:00+00:00",
        slot_duration="PT60M",
        buyer_first="Ada",
        buyer_last="Lovelace",
        buyer_email="ada@booking-agent.test",
        currency="USD",
    )
    body = build_paid_bookings_create_body(
        **common,
        booking_extras={"recipient": {"first_name": "Noa", "last_name": "Levi"},
                        "booking_question_submissions": None},
        buyer_extras={"phone_number": "+15555550100"},
    )
    assert body["booking"]["recipient"] == {"first_name": "Noa", "last_name": "Levi"}
    assert "booking_question_submissions" not in body["booking"]
    assert body["buyer"]["phone_number"] == "+15555550100"
    assert body == {**body, **build_paid_bookings_create_body(
        **common,
        booking_extras={"recipient": {"first_name": "Noa", "last_name": "Levi"}},
        buyer_extras={"phone_number": "+15555550100"},
    )}
    plain = build_paid_bookings_create_body(**common, booking_extras={}, buyer_extras=None)
    assert "recipient" not in plain["booking"]
    assert "phone_number" not in plain["buyer"]


def test_validate_create_checkout_ready_requires_status():
    with pytest.raises(UcpProfileValidationError, match="ready_for_complete"):
        validate_create_checkout_ready({"status": "pending"})


def test_validate_create_checkout_ready_allows_incomplete_pending_payment_action():
    """USP §7.5 step 4: `incomplete` with only a pending payment action is not fatal."""
    validate_create_checkout_ready(
        {
            "status": "incomplete",
            "messages": [
                {
                    "type": "error",
                    "code": "actions_pending",
                    "content": "Non-payment actions must be completed before payment can proceed.",
                    "path": "$.booking.actions",
                    "severity": "requires_buyer_input",
                }
            ],
            "booking": {
                "booking_id": "bkg_1",
                "actions": [{"type": "payment", "status": "pending"}],
            },
        }
    )


def test_validate_create_checkout_ready_blocks_incomplete_with_non_payment_action():
    """A genuine non-payment action (e.g. a waiver) pending is a hard stop (USP §7.5 step 5)."""
    with pytest.raises(MandatoryAcceptancePending, match="non-payment"):
        validate_create_checkout_ready(
            {
                "status": "incomplete",
                "booking": {
                    "booking_id": "bkg_1",
                    "actions": [{"type": "waiver", "status": "pending"}],
                },
            }
        )


def test_validate_create_checkout_ready_blocks_requires_escalation():
    with pytest.raises(MandatoryAcceptancePending, match="requires_escalation"):
        validate_create_checkout_ready(
            {
                "status": "requires_escalation",
                "continue_url": "https://merchant.example/waiver",
                "links": [{"type": "terms", "url": "https://example.com/t"}],
                "booking": {"booking_id": "bkg_1"},
            }
        )


def test_validate_ucp_checkout_completed_step_20():
    with pytest.raises(UcpProfileValidationError, match="completed"):
        validate_ucp_checkout_completed({"status": "ready_for_complete"})
    order_id = validate_ucp_checkout_completed(
        {
            "status": "completed",
            "order_id": "ord_123",
            "totals": [{"type": "total", "amount": 100}],
            "links": [{"type": "terms", "url": "https://example.com/t"}],
            "booking": {"booking_status": "confirmed", "booking_id": "b1"},
        }
    )
    assert order_id == "ord_123"


def test_validate_ucp_checkout_canceled_returns_attached_booking():
    booking = validate_ucp_checkout_canceled(
        {
            "status": "canceled",
            "booking": {
                "booking_id": "b1",
                "booking_status": "canceled",
            },
        },
        expected_booking_id="b1",
    )

    assert booking == {
        "booking_id": "b1",
        "booking_status": "canceled",
    }


def test_validate_ucp_checkout_canceled_allows_omitted_booking():
    assert (
        validate_ucp_checkout_canceled(
            {"status": "canceled"},
            expected_booking_id="b1",
        )
        is None
    )


@pytest.mark.parametrize(
    "checkout, message",
    [
        (
            {"status": "completed", "booking": {"booking_id": "b1", "booking_status": "canceled"}},
            "status 'canceled'",
        ),
        (
            {"status": "canceled", "booking": {"booking_id": "b2", "booking_status": "canceled"}},
            "expected booking 'b1'",
        ),
        (
            {"status": "canceled", "booking": {"booking_id": "b1", "booking_status": "pending"}},
            "booking_status 'canceled'",
        ),
    ],
)
def test_validate_ucp_checkout_canceled_rejects_incomplete_terminal_result(
    checkout, message,
):
    with pytest.raises(UcpProfileValidationError, match=message):
        validate_ucp_checkout_canceled(checkout, expected_booking_id="b1")


@pytest.mark.asyncio
async def test_complete_checkout_retries_500_with_stable_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booking_agent.ucp_checkout import UcpCheckoutClient

    requests: list[httpx.Request] = []
    ok = {
        "status": "completed",
        "totals": [{"type": "total", "amount": 100}],
        "links": [{"rel": "self", "href": "https://merchant.example/ucp/v1/checkout-sessions/cs_1"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(500, text="upstream")
        return httpx.Response(200, json=ok)

    transport = httpx.MockTransport(handler)

    def fake_traced(**kwargs):
        merged = dict(kwargs)
        merged["transport"] = transport
        from booking_agent.http_trace import traced_async_client as real
        return real(**merged)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("booking_agent.ucp_checkout.traced_async_client", fake_traced)
    monkeypatch.setattr("booking_agent.http_retry.asyncio.sleep", _no_sleep)

    client = UcpCheckoutClient("https://merchant.example/ucp/v1")
    data = await client.complete_checkout(
        "cs_1",
        {"payment": {"instruments": []}},
        idempotency_key="stable-key-1",
    )
    assert data["status"] == "completed"
    assert len(requests) == 2
    assert requests[0].headers["Idempotency-Key"] == "stable-key-1"
    assert requests[1].headers["Idempotency-Key"] == "stable-key-1"


_FIXED_TERM = {
    "id": "pt_fixed_deposit",
    "title": "Deposit now, balance at the appointment",
    "schedules": [
        {
            "id": "sched_deposit",
            "type": "immediate",
            "description": {"plain": "1000 due now"},
            "amount": 1000,
        },
        {
            "id": "sched_balance",
            "type": "at_service",
            "description": {"plain": "9000 due at the appointment"},
            "due_at": "2026-10-14T10:00:00-04:00",
            "amount": 9000,
        },
    ],
}


def _deposit_checkout(**overrides):
    checkout = {
        "id": "checkout_fixed_deposit_001",
        "status": "ready_for_complete",
        "currency": "USD",
        "totals": [{"type": "total", "amount": 10000}],
        "payment": {
            "selected_term_id": "pt_fixed_deposit",
            "terms": [_FIXED_TERM],
        },
        "booking": {
            "slot": {"start": "2026-10-14T10:00:00-04:00"},
        },
    }
    checkout.update(overrides)
    return checkout


def test_validate_payment_terms_fixed_deposit():
    term = validate_payment_terms(
        _deposit_checkout(),
        negotiated_capabilities={"dev.ucp.common.payment.terms": ["2026-08-25"]},
        require_terms=True,
        slot_start="2026-10-14T10:00:00-04:00",
    )
    assert term["id"] == "pt_fixed_deposit"


def test_validate_payment_terms_rejects_full_total_immediate():
    bad_term = {
        **_FIXED_TERM,
        "schedules": [
            {**_FIXED_TERM["schedules"][0], "amount": 10000},
            {**_FIXED_TERM["schedules"][1], "amount": 0},
        ],
    }
    checkout = _deposit_checkout()
    checkout["payment"]["terms"] = [bad_term]
    with pytest.raises(UcpProfileValidationError, match="immediate"):
        validate_payment_terms(
            checkout,
            negotiated_capabilities={"dev.ucp.common.payment.terms": ["2026-08-25"]},
            require_terms=True,
        )


def test_validate_payment_terms_rejects_changed_term():
    other = {**_FIXED_TERM, "id": "pt_other"}
    with pytest.raises(UcpProfileValidationError, match="changed"):
        validate_payment_terms(
            _deposit_checkout(),
            negotiated_capabilities={"dev.ucp.common.payment.terms": ["2026-08-25"]},
            persisted_term=other,
        )


def _completed_with_selected_id(selected_term_id):
    """Wix complete shape: checkout id plus an order pointer, no accepted_term."""
    return {
        "status": "completed",
        "payment": {"selected_term_id": selected_term_id},
        "order": {
            "id": "ord_1",
            "permalink_url": "https://biz.example/orders/ord_1",
        },
    }


def test_completed_checkout_accepts_matching_selected_term_id_without_accepted_term():
    validate_completed_checkout_selected_term(
        _completed_with_selected_id("pt_fixed_deposit"),
        _FIXED_TERM,
    )


@pytest.mark.parametrize(
    "payment",
    [
        None,
        {},
        {"selected_term_id": None},
        {"selected_term_id": ""},
        {"selected_term_id": 1},
    ],
)
def test_completed_checkout_rejects_missing_selected_term_id(payment):
    checkout = _completed_with_selected_id("pt_fixed_deposit")
    if payment is None:
        checkout.pop("payment")
    else:
        checkout["payment"] = payment
    with pytest.raises(UcpProfileValidationError, match="missing payment.selected_term_id"):
        validate_completed_checkout_selected_term(checkout, _FIXED_TERM)


def test_completed_checkout_rejects_a_different_selected_term_id():
    with pytest.raises(UcpProfileValidationError, match="does not match"):
        validate_completed_checkout_selected_term(
            _completed_with_selected_id("pt_other"),
            _FIXED_TERM,
        )


def test_completed_checkout_ignores_a_different_order_accepted_term():
    checkout = _completed_with_selected_id("pt_fixed_deposit")
    checkout["order"]["payment"] = {"accepted_term": {**_FIXED_TERM, "id": "pt_other"}}
    validate_completed_checkout_selected_term(checkout, _FIXED_TERM)



