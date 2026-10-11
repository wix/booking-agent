from booking_agent.headless import service_from_registry_hit
from booking_agent.models.platform import ServiceSearchResult
from booking_agent.models.wire import PricingModel, RegistryBusinessInfo
from booking_agent_cli.catalog import discover_summary_from_service


def test_service_search_result_accepts_spec_pricing_field():
    hit = ServiceSearchResult.model_validate(
        {
            "serviceId": "5d128c4f-46c9-4ddd-be51-2c81c073843f",
            "serviceName": "Emergency Window AC House Call",
            "business": {
                "id": "reg_fa3665eadb4b4dc6bbbc57a418e5614a",
                "profileUrl": "https://www.wix.com/usp/fa3665ea-db4b-4dc6-bbbc-57a418e5614a/.well-known/ucp",
                "name": "Rio Colorado Cooling",
            },
            "category": "Our Services",
            "durationMinutes": 60,
            "pricing": {
                "model": "fixed",
                "amount": "1000",
                "currency": "USD",
            },
        }
    )
    assert hit.price is not None
    assert hit.price.amount == 1000
    assert hit.price.currency == "USD"
    assert hit.price.model == "fixed"


def test_service_from_registry_hit_uses_registry_pricing_for_discover_display():
    hit = ServiceSearchResult.model_validate(
        {
            "serviceId": "6f162589-249f-43d1-a0b2-1554a18846b8",
            "serviceName": "Mobile Window AC Restoration",
            "business": {
                "id": "reg_f2ded8cb567d41cea653bc517e77b820",
                "profileUrl": "https://ranya28.wixsite.com/cool-desert-tech/.well-known/ucp",
                "name": "Cool Desert Tech",
            },
            "category": "Our Services",
            "durationMinutes": 60,
            "pricing": {
                "model": "fixed",
                "amount": "300",
                "currency": "USD",
            },
        }
    )
    service = service_from_registry_hit(hit)
    assert service.pricing.model is PricingModel.FIXED
    assert service.pricing.amount == 300
    summary = discover_summary_from_service(service)
    assert summary["price_display"] == "$3.00"
    assert summary["price_minor_units"] == 300
    assert summary["catalog_source"] == "registry"


def test_service_from_registry_hit_keeps_legacy_price_field():
    hit = ServiceSearchResult(
        service_id="svc-a",
        service_name="AC Repair",
        business=RegistryBusinessInfo(
            id="biz-1",
            name="HVAC Koby",
            profile_url="https://hvackoby.com/.well-known/ucp",
        ),
        category="hvac",
        price={"amount": 100, "currency": "USD", "model": "fixed"},
    )
    service = service_from_registry_hit(hit)
    assert service.pricing.amount == 100
    assert service.pricing.model is PricingModel.FIXED


def test_discover_summary_includes_registry_search_result():
    from booking_agent.headless import attach_registry_hit

    hit = ServiceSearchResult(
        service_id="svc-a",
        service_name="AC Repair",
        business=RegistryBusinessInfo(
            id="biz-1",
            name="HVAC Koby",
            profile_url="https://hvackoby.com/.well-known/ucp",
        ),
        category="hvac",
        price={"amount": 100, "currency": "USD", "model": "fixed"},
        timezone="America/New_York",
    )
    service = attach_registry_hit(service_from_registry_hit(hit), hit, rank=2)
    summary = discover_summary_from_service(service)
    assert summary["registry_search_result"] is not None
    assert summary["registry_search_result"]["serviceId"] == "svc-a"
    assert summary["registry_search_result"]["business"]["name"] == "HVAC Koby"
    assert summary["search_rank"] == 2
