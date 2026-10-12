"""Targeted regression tests for USP spec conformance fixes."""
import pytest
from pydantic import ValidationError

from booking_agent.models.wire import (
    RegistryBusinessInfo,
    ServiceCategory,
    USPService,
    primary_service_category,
    project_category_string,
)


def test_registry_business_info_accepts_spec_field_name():
    biz = RegistryBusinessInfo.model_validate(
        {"id": "biz_1", "name": "Acme", "profile_url": "https://acme.example/.well-known/usp"}
    )
    assert biz.profile_url == "https://acme.example/.well-known/usp"
    assert biz.deployment_mode == "standalone"


def test_registry_business_info_accepts_legacy_wix_field_name():
    biz = RegistryBusinessInfo.model_validate(
        {"id": "biz_2", "name": "Wix Biz", "usp_profile_url": "https://wix.example/.well-known/usp"}
    )
    assert biz.profile_url == "https://wix.example/.well-known/usp"
    assert biz.usp_profile_url == "https://wix.example/.well-known/usp"


def test_registry_business_info_accepts_camel_case_alias():
    biz = RegistryBusinessInfo.model_validate(
        {"id": "biz_3", "name": "Camel", "uspProfileUrl": "https://camel.example/.well-known/usp"}
    )
    assert biz.profile_url == "https://camel.example/.well-known/usp"


def _minimal_service(**overrides):
    base = {
        "id": "svc_1",
        "business_id": "biz_1",
        "name": "Haircut",
        "type": "appointment",
        "duration": {"fixed": "PT30M"},
        "pricing": {"model": "fixed", "amount": 100, "currency": "USD"},
        "channel": {"type": "at_business_location"},
        "policies": {
            "cancellation": {"allowed": True},
            "rescheduling": {"allowed": True},
            "confirmation_mode": "auto",
            "requires_payment": False,
        },
    }
    base.update(overrides)
    return USPService.model_validate(base)


def test_service_coerces_legacy_singular_category_into_categories():
    service = _minimal_service(category={"id": "cat_hair", "name": "Hair"})
    assert service.categories is not None
    assert len(service.categories) == 1
    assert service.categories[0].taxonomy == "merchant"
    assert service.categories[0].id == "cat_hair"
    assert service.categories[0].name == "Hair"
    assert service.category is service.categories[0]


def test_service_accepts_enriched_multi_taxonomy_categories():
    service = _minimal_service(
        categories=[
            {
                "taxonomy": "merchant",
                "id": "cat_hair",
                "name": "Hair",
                "value": "beauty > hair > haircut",
                "primary": True,
            },
            {
                "taxonomy": "google_business_profile",
                "value": "gcid:hair_salon",
            },
        ]
    )
    assert len(service.categories) == 2
    assert primary_service_category(service.categories).id == "cat_hair"
    assert project_category_string(categories=service.categories) == "Hair"


def test_primary_service_category_falls_back_to_sole_merchant():
    cats = [
        ServiceCategory(taxonomy="google_business_profile", value="gcid:hair_salon"),
        ServiceCategory(taxonomy="merchant", id="cat_hair", name="Hair"),
    ]
    assert primary_service_category(cats).taxonomy == "merchant"


def test_project_category_string_pick_order():
    assert (
        project_category_string(
            categories=[ServiceCategory(taxonomy="merchant", id="only_id")],
            service_type="appointment",
        )
        == "only_id"
    )
    assert project_category_string(categories=None, service_type="appointment") == "appointment"


def test_external_taxonomy_requires_value():
    with pytest.raises(ValidationError):
        ServiceCategory(taxonomy="google_business_profile", name="Hair Salon")


def test_service_rejects_empty_category_identity():
    with pytest.raises(ValidationError):
        ServiceCategory(taxonomy="merchant")
