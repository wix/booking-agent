"""Bundled USP services for offline registry tests.

Keeps the Koby's HVAC entries
(for the AC repair Yuma test path) and a couple of barbershops for variety.
"""
from datetime import datetime, timezone
from typing import Dict, List

from booking_agent.models.wire import (
    AvailabilityHint,
    AvailabilitySlotBitmap,
    BookingWindow,
    CancellationPolicy,
    Channel,
    Coordinates,
    Duration,
    Location,
    Pricing,
    RegistryBusinessInfo,
    ReschedulingPolicy,
    ResourceOption,
    ResourceRequirement,
    ServiceCategory,
    ServiceImage,
    ServicePolicies,
    USPService,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tomorrow_iso_date() -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")


MOCK_SERVICES: List[USPService] = [
    USPService(
        id="svc_desert_sun_classic",
        business_id="biz_desert_sun",
        name="Classic Men's Haircut",
        description=(
            "A timeless men's haircut including wash, cut, and style. "
            "Perfect for maintaining a clean, professional look."
        ),
        type="appointment",
        categories=[ServiceCategory(taxonomy="merchant", id="cat_hair", name="Hair")],
        duration=Duration(fixed="PT30M"),
        # amount is minor currency units (USD cents): 100 = $1.00
        pricing=Pricing(model="fixed", amount=100, currency="USD"),
        locations=[
            Location(
                id="loc_desert_sun",
                name="Desert Sun Barbershop",
                address="2500 S Pacific Ave, Yuma, AZ 85365",
                coordinates=Coordinates(lat=32.6625, lng=-114.6180),
            )
        ],
        resources=[
            ResourceRequirement(
                type="staff",
                name="Barber",
                selectable=True,
                options=[
                    ResourceOption(
                        id="staff_carlos",
                        name="Carlos Rivera",
                        description="15 years experience, specializes in classic cuts",
                    )
                ],
            )
        ],
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True, free_cancellation_until="PT2H"),
            rescheduling=ReschedulingPolicy(allowed=True, free_reschedule_until="PT2H", max_reschedules=2),
            booking_window=BookingWindow(min_advance="PT1H", max_advance="P30D", slot_interval="PT30M"),
            confirmation_mode="auto",
            requires_payment=True,
            payment_timing="at_booking",
        ),
        images=[ServiceImage(url="https://example.com/desert-sun.jpg", alt="Classic haircut", type="hero")],
        availability_hint=AvailabilityHint(
            summary=(
                "Good availability this morning and late afternoon. Midday is "
                "mostly booked, and 90-minute sessions are limited to 9:00-9:30 "
                "and after 16:00."
            ),
            generated_at="2026-03-14T07:30:00-04:00",
            valid_until="2026-03-14T19:30:00-04:00",
            next_available_date="2026-03-14",
            slot_bitmaps=[
                AvailabilitySlotBitmap(
                    duration="PT60M",
                    starts_at="2026-03-14T09:00:00-04:00",
                    start_interval="PT30M",
                    slot_count=17,
                    encoding="roaring32-portable-base64",
                    bitmap="OjAAAAEAAAAAAAcAEAAAAAAAAQACAAYACgAOAA8AEAA=",
                ),
                AvailabilitySlotBitmap(
                    duration="PT90M",
                    starts_at="2026-03-14T09:00:00-04:00",
                    start_interval="PT30M",
                    slot_count=16,
                    encoding="roaring32-portable-base64",
                    bitmap="OjAAAAEAAAAAAAMAEAAAAAAAAQAOAA8A",
                ),
            ],
        ),
        business=RegistryBusinessInfo(
            id="biz_desert_sun",
            name="Desert Sun Barbershop",
            usp_profile_url="https://desert-sun-barbershop.example.com/.well-known/usp",
        ),
    ),
    USPService(
        id="e4d4b7a4-0e66-4104-891b-ba5bb4a04092",
        business_id="051412f9-c8ed-4392-9e04-21b5e464befe",
        name="AC Repair",
        description=(
            "Ensure your air conditioning system runs smoothly with our expert AC repair services. "
            "Our experienced technicians diagnose and fix issues promptly to keep your environment comfortable."
        ),
        type="appointment",
        categories=[
            ServiceCategory(
                taxonomy="merchant",
                id="d4f6b59d-b04a-421b-a8fb-52102c779372",
                name="Our Services",
            )
        ],
        duration=Duration(fixed="PT1H"),
        # amount is minor currency units (USD cents): 100 = $1.00 (matches live Koby's HVAC catalog)
        pricing=Pricing(model="fixed", amount=100, currency="USD"),
        locations=[Location(id="loc_kobys_hvac", name="Yuma, AZ")],
        resources=[
            ResourceRequirement(
                type="staff",
                name="Staff",
                selectable=False,
                options=[ResourceOption(id="78aa25a4-a5ba-4652-ae42-93904c69b3cd")],
            )
        ],
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            booking_window=BookingWindow(min_advance="PT1H", max_advance="P30D", slot_interval="PT30M"),
            confirmation_mode="auto",
            requires_payment=True,
            payment_timing="at_booking",
        ),
        images=[ServiceImage(url="https://example.com/ac-repair.jpg", alt="AC repair", type="hero")],
        availability_hint=AvailabilityHint(
            summary="Available next week 10:00-18:00 local",
            generated_at=_now_iso(),
            next_available_date=_tomorrow_iso_date(),
        ),
        business=RegistryBusinessInfo(
            id="051412f9-c8ed-4392-9e04-21b5e464befe",
            name="Koby's HVAC Services",
            usp_profile_url="https://kobym9.wixsite.com/hvac-koby/.well-known/usp",
        ),
    ),
    USPService(
        id="03c8286d-7da3-42df-a414-10db326948a8",
        business_id="051412f9-c8ed-4392-9e04-21b5e464befe",
        name="Duct Cleaning",
        description="Comprehensive duct cleaning. Free in-home estimate.",
        type="appointment",
        categories=[
            ServiceCategory(
                taxonomy="merchant",
                id="d4f6b59d-b04a-421b-a8fb-52102c779372",
                name="Our Services",
            )
        ],
        duration=Duration(fixed="PT1H30M"),
        pricing=Pricing(model="free", currency="USD"),  # spec: amount MUST NOT be present when model=free
        locations=[Location(id="loc_kobys_hvac", name="Yuma, AZ")],
        resources=[
            ResourceRequirement(
                type="staff",
                name="Staff",
                selectable=False,
                options=[ResourceOption(id="78aa25a4-a5ba-4652-ae42-93904c69b3cd")],
            )
        ],
        channel=Channel(type="at_business_location"),
        policies=ServicePolicies(
            cancellation=CancellationPolicy(allowed=True),
            rescheduling=ReschedulingPolicy(allowed=True),
            confirmation_mode="auto",
            requires_payment=False,
        ),
        business=RegistryBusinessInfo(
            id="051412f9-c8ed-4392-9e04-21b5e464befe",
            name="Koby's HVAC Services",
            usp_profile_url="https://kobym9.wixsite.com/hvac-koby/.well-known/usp",
        ),
    ),
]

SERVICE_BY_ID: Dict[str, USPService] = {s.id: s for s in MOCK_SERVICES}


def set_zero_price_free_service(monkeypatch, service_id: str) -> None:
    """Make the mock business serve and book ``service_id`` as free (amount 0, no payment).

    Patching the shared catalog entry, not just ``get_service``, keeps
    ``create_booking`` consistent so the booking is confirmed without a payment action.
    """
    service = SERVICE_BY_ID[service_id]
    policies = service.policies.model_copy(
        update={"requires_payment": False, "payment_timing": None},
    )
    pricing = service.pricing.model_copy(
        update={"amount": 0, "price_range": None, "deposit": None},
    )
    monkeypatch.setitem(
        SERVICE_BY_ID, service_id,
        service.model_copy(update={"policies": policies, "pricing": pricing}),
    )
