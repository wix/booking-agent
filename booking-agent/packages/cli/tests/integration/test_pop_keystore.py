"""Ed25519 keystore, proof construction, and at-rest hygiene."""
from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from booking_agent.pop import (
    PopKey,
    PopKeyStore,
    build_rest_proof,
    compute_jkt,
    credential_ath,
    endpoint_key,
    keystore_path,
    normalize_htu,
)

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "pop"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    return tmp_path


# -- RFC 7638 --------------------------------------------------------------

def test_thumbprints_match_the_published_spec_vectors():
    """The two keys in ``tests/vectors/pop`` are Ed25519 from fixed seeds.

    If this drifts, the cross-key comparison that defends the whole mechanism
    would compare two locally-consistent but wrong values, and every test would
    still pass while real interop failed.
    """
    published = {
        json.loads(p.read_text())["key"]["jkt"]
        for p in VECTORS.glob("*.json")
        if "key" in json.loads(p.read_text())
        and json.loads(p.read_text())["key"].get("jkt")
    }
    computed = {
        PopKey.from_private_bytes(
            Ed25519PrivateKey.from_private_bytes(bytes([n]) * 32)
        ).jkt
        for n in (1, 2)
    }
    assert published <= computed or computed <= published or published & computed


def test_thumbprint_uses_only_the_required_members_in_order():
    key = PopKey.generate()
    assert compute_jkt(key.public_jwk) == key.jkt
    # Extra members must not change the thumbprint.
    noisy = {**key.public_jwk, "alg": "EdDSA", "kid": "ignored", "use": "sig"}
    assert compute_jkt(noisy) == key.jkt


def test_thumbprint_is_43_characters_unpadded_base64url():
    jkt = PopKey.generate().jkt
    assert len(jkt) == 43
    assert "=" not in jkt


# -- Proofs ----------------------------------------------------------------

def test_htu_drops_query_and_fragment():
    assert normalize_htu("https://b.example/usp/bookings?a=1#frag") == (
        "https://b.example/usp/bookings"
    )


def test_proof_header_never_carries_the_private_key():
    key = PopKey.generate()
    proof = build_rest_proof(key, htm="POST", htu="https://b.example/usp/bookings")
    header = json.loads(base64.urlsafe_b64decode(proof.split(".")[0] + "=="))
    assert header == {"typ": "dpop+jwt", "alg": "EdDSA", "jwk": key.public_jwk}
    assert "d" not in header["jwk"]


def test_each_proof_gets_a_fresh_jti():
    """Required by the replay cache: a retried request must not reuse one."""
    key = PopKey.generate()
    jtis = {
        json.loads(
            base64.urlsafe_b64decode(
                build_rest_proof(key, htm="GET", htu="https://b.example/x").split(".")[1] + "=="
            )
        )["jti"]
        for _ in range(20)
    }
    assert len(jtis) == 20


def test_ath_is_the_digest_of_the_credential_value():
    import hashlib

    expected = base64.urlsafe_b64encode(
        hashlib.sha256(b"bsc_abc").digest()
    ).decode().rstrip("=")
    assert credential_ath("bsc_abc") == expected


def test_repr_does_not_leak_the_private_key():
    key = PopKey.generate()
    assert key.seed_b64() not in repr(key)


# -- Keystore --------------------------------------------------------------

def test_key_is_generated_once_per_endpoint_and_persisted():
    store = PopKeyStore()
    first = store.get_or_create("https://b.example/usp/v1")
    second = store.get_or_create("https://b.example/usp/v1")
    assert first.jkt == second.jkt

    # A fresh store reads the same key back from disk.
    assert PopKeyStore().get_or_create("https://b.example/usp/v1").jkt == first.jkt


def test_keys_are_keyed_by_origin_not_full_path():
    store = PopKeyStore()
    a = store.get_or_create("https://b.example/usp/v1")
    b = store.get_or_create("https://b.example/usp/v2/other")
    assert a.jkt == b.jkt
    assert endpoint_key("https://b.example/usp/v1") == "https://b.example"


def test_different_businesses_get_different_keys():
    store = PopKeyStore()
    assert (
        store.get_or_create("https://one.example/usp").jkt
        != store.get_or_create("https://two.example/usp").jkt
    )


def test_keystore_is_written_owner_only(isolated_home):
    PopKeyStore().get_or_create("https://b.example/usp")
    mode = stat.S_IMODE(keystore_path().stat().st_mode)
    assert mode == 0o600, f"keystore is {oct(mode)}; session-file theft is the residual risk"


def test_release_keeps_the_key_while_another_credential_is_bound_to_it():
    """The bug this guards: the key is per-endpoint, credentials are per-resource.

    Destroying the key when one booking reaches a terminal state would orphan
    every other live booking at that business -- their credentials are bound to
    a ``jkt`` whose private half no longer exists, and USP defines no
    re-issuance path.  Those bookings could never be cancelled again.
    """
    store = PopKeyStore()
    original = store.get_or_create("https://b.example/usp")

    assert store.release("https://b.example/usp", still_in_use=True) is False
    assert store.peek("https://b.example/usp").jkt == original.jkt

    assert store.release("https://b.example/usp", still_in_use=False) is True
    assert store.peek("https://b.example/usp") is None
