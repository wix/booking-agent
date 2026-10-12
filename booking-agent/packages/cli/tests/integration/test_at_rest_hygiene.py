"""Everything under ``~/.booking-agent`` is owner-only, and secrets never reach the trace.

Session-file theft is the dominant residual risk of ``platform_key_pop``: the
credential is useless without the key, but both live in this directory.  The
0600 mitigation the design leans on was not implemented before this change --
all three stores were written at the default umask.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from booking_agent import http_trace, profile_cache
from booking_agent.discovery import USPBusinessInfo, USPBusinessProfile
from booking_agent.pop import PopKey, PopKeyStore, build_rest_proof, credential_ath
from booking_agent.secure_file import write_private_text


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    return tmp_path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_private_text_is_owner_only(tmp_path):
    target = tmp_path / "secret.json"
    write_private_text(target, '{"a": 1}')
    assert _mode(target) == 0o600
    assert target.read_text() == '{"a": 1}'


def test_write_private_text_replaces_atomically(tmp_path):
    """No window in which the file exists world-readable, and no partial content."""
    target = tmp_path / "secret.json"
    write_private_text(target, "first")
    write_private_text(target, "second")
    assert target.read_text() == "second"
    assert _mode(target) == 0o600
    assert not list(tmp_path.glob(".secret.json.*")), "temp file left behind"


def test_profile_cache_is_owner_only(isolated_home):
    profile_cache.write_cached_profile(
        "https://b.example/.well-known/usp",
        USPBusinessProfile(
            version="2026-08-14",
            services={},
            capabilities={},
            checkout_systems=[],
            business=USPBusinessInfo(name="Biz"),
        ),
        None,
    )
    assert _mode(profile_cache.cache_path()) == 0o600


def test_pop_keystore_is_owner_only(isolated_home):
    PopKeyStore().get_or_create("https://b.example/usp")
    assert _mode(isolated_home / "pop_keys.json") == 0o600


def test_session_directory_is_owner_only(isolated_home):
    PopKeyStore().get_or_create("https://b.example/usp")
    assert _mode(isolated_home) == 0o700


def test_profile_cache_discards_entries_from_an_older_format(isolated_home):
    """A pre-v2 entry has no authorization policy recorded, which is
    indistinguishable from a business that publishes none -- and that routes
    privileged calls onto the fail-closed path."""
    profile_cache.write_cached_profile(
        "https://b.example/.well-known/usp",
        USPBusinessProfile(
            version="2026-08-14", services={}, capabilities={},
            checkout_systems=[], business=USPBusinessInfo(name="Biz"),
        ),
        None,
    )
    path = profile_cache.cache_path()
    store = json.loads(path.read_text())
    for entry in store.values():
        entry.pop("format_version", None)
    write_private_text(path, json.dumps(store))

    assert profile_cache.read_cached_profile("https://b.example/.well-known/usp") is None


# -- Trace redaction -------------------------------------------------------

@pytest.mark.asyncio
async def test_trace_redacts_the_proof_and_the_credential(isolated_home, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_HTTP_TRACE", "1")
    key = PopKey.generate()
    credential = "bsc_supersecretcredentialvalue"
    proof = build_rest_proof(
        key, htm="POST", htu="https://b.example/usp/bookings/1/cancel",
        ath=credential_ath(credential),
    )

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    async with http_trace.traced_async_client(transport=transport) as client:
        await client.post(
            "https://b.example/usp/bookings/1/cancel",
            headers={"Authorization": f"DPoP {credential}", "DPoP": proof},
        )

    log = http_trace.trace_path().read_text()
    assert credential not in log, "the credential value reached the plaintext trace log"
    assert proof not in log, "the proof, which embeds our public JWK, reached the trace log"
    assert key.seed_b64() not in log, "the private key reached the trace log"
    assert "<redacted>" in log


@pytest.mark.asyncio
async def test_trace_redacts_spt_and_buyer_pii_in_body(isolated_home, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_HTTP_TRACE", "1")
    spt = "spt_supersecretpaymenttoken"
    body = json.dumps({
        "payment": {
            "instruments": [{"credential": {"token": spt}}],
        },
        "buyer": {"email": "ada@test.com", "name": "Ada"},
    })
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text='{"ok":true}')
    )
    async with http_trace.traced_async_client(transport=transport) as client:
        await client.post(
            "https://checkout.example/complete",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    log = http_trace.trace_path().read_text()
    assert spt not in log
    assert "ada@test.com" not in log
    assert "<redacted>" in log


def test_trace_log_itself_is_owner_only(isolated_home, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_HTTP_TRACE", "1")
    http_trace._append({"hello": "world"})
    assert _mode(http_trace.trace_path()) == 0o600
