import json
import time

import pytest

from booking_agent.discovery import USPBusinessInfo, USPBusinessProfile, USPTransportBinding
from booking_agent.profile_cache import cache_path, read_cached_profile, write_cached_profile


def _sample_profile() -> USPBusinessProfile:
    return USPBusinessProfile(
        version="1.0",
        services={
            "dev.usp-protocol.services": [
                USPTransportBinding(
                    version="1.0",
                    transport="rest",
                    endpoint="https://biz.example.com/usp/v1",
                )
            ]
        },
        capabilities={},
        checkout_systems=[],
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )


def test_write_and_read_cached_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    url = "https://biz.example.com/.well-known/ucp"
    profile = _sample_profile()
    write_cached_profile(url, profile, None)
    cached = read_cached_profile(url)
    assert cached is not None
    prof, uctx = cached
    assert prof.business.name == "Biz"
    assert uctx is None
    assert cache_path().is_file()


def test_expired_cache_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("BOOKING_AGENT_PROFILE_CACHE_TTL_SECONDS", "1")
    url = "https://biz.example.com/.well-known/ucp"
    write_cached_profile(url, _sample_profile(), None)
    store = json.loads(cache_path().read_text())
    key = next(iter(store))
    store[key]["cached_at"] = time.time() - 10
    cache_path().write_text(json.dumps(store))
    assert read_cached_profile(url) is None
