"""USP #40: DeliveryAddress + channel rename (at_business_location / at_buyer_location)."""

from booking_agent.models.wire import (
    Booking,
    BookingStatus,
    Buyer,
    Channel,
    ChannelType,
    Coordinates,
    DeliveryAddress,
    SlotReference,
)


def test_channel_type_enum_matches_catalog_schema():
    assert [m.value for m in ChannelType] == [
        "at_business_location",
        "at_buyer_location",
        "virtual",
        "phone",
        "hybrid",
    ]


def test_channel_accepts_service_area_for_buyer_location():
    ch = Channel(
        type=ChannelType.AT_BUYER_LOCATION,
        service_area="Greater Phoenix metro",
        instructions="Park in driveway",
    )
    dumped = ch.model_dump(by_alias=True, exclude_none=True)
    assert dumped["type"] == "at_buyer_location"
    assert dumped["serviceArea"] == "Greater Phoenix metro"


def test_delivery_address_required_fields_and_optional_coords():
    addr = DeliveryAddress(
        line1="3194 West Rome St.",
        city="Yuma",
        country="US",
        region="AZ",
        postal_code="85364",
        coordinates=Coordinates(lat=32.69, lng=-114.63),
    )
    dumped = addr.model_dump(by_alias=True, exclude_none=True)
    assert dumped["line1"] == "3194 West Rome St."
    assert dumped["postalCode"] == "85364"
    assert dumped["coordinates"] == {"lat": 32.69, "lng": -114.63}


def test_booking_echoes_delivery_address():
    booking = Booking(
        id="bkg_1",
        service_id="svc_1",
        service_name="AC Repair",
        slot=SlotReference(
            id="slot_1",
            start="2026-08-07T10:00:00Z",
            end="2026-08-07T11:00:00Z",
            duration="PT1H",
        ),
        buyer=Buyer(first_name="Ran", last_name="Y", email="ran@example.com"),
        party_size=1,
        delivery_address=DeliveryAddress(
            line1="3194 West Rome St.",
            city="Yuma",
            country="US",
        ),
        status=BookingStatus.CONFIRMED,
        confirmation_mode="auto",
        created_at="2026-08-06T12:00:00Z",
        updated_at="2026-08-06T12:00:00Z",
    )
    dumped = booking.model_dump(by_alias=True, exclude_none=True)
    assert dumped["deliveryAddress"]["line1"] == "3194 West Rome St."
    assert dumped["deliveryAddress"]["city"] == "Yuma"
    assert dumped["deliveryAddress"]["country"] == "US"


def test_build_paid_bookings_create_body_includes_delivery_address():
    from booking_agent.models.wire import (
        CancellationPolicy,
        Duration,
        Pricing,
        PricingModel,
        ReschedulingPolicy,
        ServicePolicies,
        USPService,
    )
    from booking_agent.ucp_checkout import build_paid_bookings_create_body

    service = USPService(
        id="svc_ac",
        business_id="biz_1",
        name="AC Repair",
        type="field_service",
        duration=Duration(fixed="PT1H"),
        pricing=Pricing(model=PricingModel.FIXED, amount=100, currency="USD"),
        channel=Channel(type=ChannelType.AT_BUYER_LOCATION),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            confirmation_mode="auto",
            requires_payment=True,
        ),
    )
    body = build_paid_bookings_create_body(
        service=service,
        slot_id="slot_1",
        slot_start="2026-08-07T10:00:00Z",
        slot_end="2026-08-07T11:00:00Z",
        slot_duration="PT1H",
        buyer_first="Ran",
        buyer_last="Y",
        buyer_email="ran@example.com",
        currency="USD",
        line_item_amount_minor=100,
        delivery_address={
            "line1": "3194 West Rome St.",
            "city": "Yuma",
            "country": "US",
        },
    )
    assert body["booking"]["delivery_address"]["line1"] == "3194 West Rome St."
    assert "notes" not in body["booking"]
