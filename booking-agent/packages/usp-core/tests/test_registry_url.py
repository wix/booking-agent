from booking_agent.config import load_config
from booking_agent import registry_url


EXPECTED_REGISTRY_URL = (
    "https://www.wixapis.com/_api/usp-registry/registry/search_services"
)


def test_registry_url_wix_production_search_unchanged():
    assert registry_url.registry_search_post_url(EXPECTED_REGISTRY_URL) == EXPECTED_REGISTRY_URL


def test_registry_url_defaults_to_wix_production(monkeypatch):
    monkeypatch.delenv("USP_REGISTRY_URL", raising=False)
    assert getattr(registry_url, "DEFAULT_REGISTRY_URL", None) == EXPECTED_REGISTRY_URL
    assert load_config().usp_registry_url == EXPECTED_REGISTRY_URL


def test_registry_url_explicit_search_services_unchanged():
    u = "https://registry.example.com/registry/search_services"
    assert registry_url.registry_search_post_url(u) == u


def test_registry_url_base_gets_spec_path():
    u = "https://registry.example.com"
    assert registry_url.registry_search_post_url(u) == (
        "https://registry.example.com/registry/search_services"
    )
