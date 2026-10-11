"""``platform_key_pop`` client behaviour (USP Section 10.1.6, spec issue #9 AC-L).

The load-bearing test is ``test_credential_replayed_with_a_different_key_is_rejected``.
Everything else guards a specific normative MUST.
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from booking_agent.authorization import (
    BookingScopedCredential,
    CredentialBindingError,
    resolve_posture,
)
from booking_agent.backend.live import LiveUSPBackend
from booking_agent.platform_profile import platform_profile_doc
from booking_agent.pop import PopKey, build_rest_proof, credential_ath
from booking_agent.rest_client import PopAuthorizationError, USPBusinessRestClient
from support.mock.pop_business import PopBusiness
from support.mock.pop_transport import (
    ENDPOINT,
    PROFILE_URI,
    MockPopBusinessTransport,
    business_profile,
)

AGENT_PROFILE = "https://profile.booking-agent.dev/platform-profile.json"
MOCK_AGENT_PROFILE = "https://mock-agent.example/platform-profile.json"
CHECKOUT_ENDPOINT = "https://checkout.mock-business.example/ucp/v1"


def _client(monkeypatch, *, key: PopKey, authenticate: bool = True, mock=None):
    mock = mock or MockPopBusinessTransport()
    mock.patch(monkeypatch)
    client = USPBusinessRestClient(
        business_profile(),
        agent_profile_url=AGENT_PROFILE,
        pop_key=key,
        authenticate=authenticate,
    )
    return client, mock


@pytest.mark.asyncio
async def test_live_backend_uses_credential_issuer_key_for_cross_binding_request():
    issuer_key = PopKey.generate()

    class TokenStore:
        @staticmethod
        def get_auth_header_name(_endpoint):
            return "Authorization"

        @staticmethod
        def has_stored_credentials(_endpoint):
            return False

    class PopKeys:
        @staticmethod
        def peek(endpoint):
            assert endpoint == CHECKOUT_ENDPOINT
            return issuer_key

        @staticmethod
        def get_or_create(_endpoint):
            raise AssertionError("must not create a USP-origin key for an issued credential")

    backend = object.__new__(LiveUSPBackend)
    backend._cfg = SimpleNamespace(booking_agent_usp_merchant_bearer_token=None)
    backend._agent_profile_url = AGENT_PROFILE
    backend._token_store = TokenStore()
    backend._posture = resolve_posture("required")
    backend._pop_keys = PopKeys()

    client = await backend._client(
        business_profile(),
        pop_key_endpoint=CHECKOUT_ENDPOINT,
    )

    assert client.endpoint == ENDPOINT
    assert client.pop_jkt == issuer_key.jkt


async def _book(client) -> tuple[str, BookingScopedCredential]:
    booking = await client.create_booking(
        service_id="svc_1", slot_id="slot_1", buyer={"name": "A", "email": "a@b.c"},
    )
    assert client.last_issued_credential is not None
    return booking.id, client.last_issued_credential


@pytest.mark.asyncio
async def test_business_fetches_keyless_profile_before_accepting_platform_key_pop(
    monkeypatch,
):
    profile = platform_profile_doc()
    serialized = json.dumps(profile).lower()
    assert not ({"keys", "signing_keys"} & profile.keys())
    for prohibited in ("mcp", "a2a", "webhook_url"):
        assert prohibited not in serialized
    assert all(
        binding["transport"] == "rest"
        for bindings in profile["usp"]["services"].values()
        for binding in bindings
    )
    mock = MockPopBusinessTransport(
        agent_profile_url=MOCK_AGENT_PROFILE,
        agent_profile_doc=profile,
    )
    mock.patch(monkeypatch)
    client = USPBusinessRestClient(
        business_profile(),
        agent_profile_url=MOCK_AGENT_PROFILE,
        pop_key=PopKey.generate(),
        authenticate=True,
    )

    booking_id, credential = await _book(client)

    assert booking_id
    assert credential.cnf is not None
    assert mock.fetched_agent_profiles == {MOCK_AGENT_PROFILE: profile}
    assert mock.business.jkt_by_profile[MOCK_AGENT_PROFILE] == credential.cnf.jkt


# ---------------------------------------------------------------------------
# The load-bearing test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_credential_replayed_with_a_different_key_is_rejected(monkeypatch):
    """Create with K1, steal the credential, present it with K2 -> MUST fail.

    This is the test the superseded design would have failed: it bound the
    credential to the ``USP-Agent`` profile URI, which an attacker also knows and
    simply asserts.

    Note what makes it hard.  K2's proof is *correctly signed* and verifies
    against the JWK in its own header, so signature verification passes.  Only
    comparing that key's RFC 7638 thumbprint against the recorded ``cnf.jkt``
    rejects it.  An implementation that checks the signature and stops accepts
    the attack.
    """
    k1, k2 = PopKey.generate(), PopKey.generate()
    assert k1.jkt != k2.jkt

    victim, mock = _client(monkeypatch, key=k1)
    booking_id, credential = await _book(victim)

    # The thief holds the leaked credential value and knows the public profile
    # URI, but not K1's private half -- so they use their own key.
    thief = USPBusinessRestClient(
        business_profile(), agent_profile_url=AGENT_PROFILE,
        pop_key=k2, authenticate=True,
    )
    with pytest.raises(PopAuthorizationError) as caught:
        await thief.cancel_booking(booking_id, credential=credential)
    assert caught.value.code == "pop_key_mismatch"

    # ...and the rightful holder can still use it.
    cancelled = await victim.cancel_booking(booking_id, credential=credential)
    assert cancelled.status.value == "canceled"


def test_vendored_cross_key_vector_matches_our_thumbprints():
    """Our RFC 7638 implementation agrees with the spec's published vectors.

    Vector 006 is the normative form of the test above.  If ``compute_jkt``
    drifted, the comparison that defends the whole mechanism would silently
    compare two wrong values and still agree with itself.
    """
    from pathlib import Path

    vectors = Path(__file__).resolve().parents[2] / "usp-core/tests/vectors/pop"
    vector = json.loads((vectors / "006-cross-key-replay.json").read_text())

    assert vector["expect"] == "reject"
    assert vector["reject_code"] == "pop_key_mismatch"

    seed = base64.urlsafe_b64decode(
        vector["key"]["private_key_d"] + "=" * (-len(vector["key"]["private_key_d"]) % 4)
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    attacker = PopKey.from_private_bytes(Ed25519PrivateKey.from_private_bytes(seed))
    assert attacker.jkt == vector["key"]["jkt"]
    # The credential in the vector is bound to the *other* key.
    assert vector["credential"]["cnf"]["jkt"] != attacker.jkt


# ---------------------------------------------------------------------------
# Anti-downgrade, replay, scope, expiry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_declared_downgrade_to_bearer_is_rejected(monkeypatch):
    """A cnf-bearing credential presented as a plain bearer MUST be rejected.

    Without this MUST the scheme is bypassable by changing one field.
    """
    key = PopKey.generate()
    client, mock = _client(monkeypatch, key=key)
    booking_id, credential = await _book(client)

    async with httpx.AsyncClient(transport=mock.transport()) as raw:
        response = await raw.post(
            f"{ENDPOINT}/bookings/{booking_id}/cancel",
            headers={"Authorization": f"Bearer {credential.value}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "pop_proof_missing"


@pytest.mark.asyncio
async def test_missing_proof_is_rejected(monkeypatch):
    key = PopKey.generate()
    client, mock = _client(monkeypatch, key=key)
    booking_id, credential = await _book(client)

    async with httpx.AsyncClient(transport=mock.transport()) as raw:
        response = await raw.post(
            f"{ENDPOINT}/bookings/{booking_id}/cancel",
            headers={"Authorization": f"DPoP {credential.value}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "pop_proof_missing"


@pytest.mark.asyncio
async def test_replayed_jti_is_rejected(monkeypatch):
    """Replay defence applies to every privileged call presenting a proof."""
    key = PopKey.generate()
    client, mock = _client(monkeypatch, key=key)
    booking_id, credential = await _book(client)

    url = f"{ENDPOINT}/bookings/{booking_id}"
    proof = build_rest_proof(
        key, htm="GET", htu=url, ath=credential_ath(credential.value),
    )
    headers = {"Authorization": f"DPoP {credential.value}", "DPoP": proof}
    async with httpx.AsyncClient(transport=mock.transport()) as raw:
        first = await raw.get(url, headers=headers)
        second = await raw.get(url, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "pop_proof_replayed"


@pytest.mark.asyncio
async def test_credential_is_scoped_to_one_booking(monkeypatch):
    key = PopKey.generate()
    client, mock = _client(monkeypatch, key=key)
    first_id, first_credential = await _book(client)
    second_id, _ = await _book(client)

    with pytest.raises(PopAuthorizationError):
        # The first booking's credential must not act on the second.
        await client.cancel_booking(second_id, credential=first_credential)


@pytest.mark.asyncio
async def test_expired_credential_is_not_sent(monkeypatch):
    """An expired credential goes to the booking_url handoff, not a 401 round trip."""
    key = PopKey.generate()
    client, mock = _client(monkeypatch, key=key)
    booking_id, _ = await _book(client)

    expired = BookingScopedCredential.model_validate({
        "value": "bsc_expired_credential_value_here",
        "expires_at": "2020-01-01T00:00:00Z",
        "cnf": {"jkt": key.jkt},
    })
    assert client._guard_scope(expired, "usp_bookings_cancel") is None


def test_credential_outside_its_scope_is_not_sent():
    key = PopKey.generate()
    client = USPBusinessRestClient(
        business_profile(), pop_key=key, authenticate=True,
    )
    narrow = BookingScopedCredential.model_validate({
        "value": "bsc_read_only_credential_value_x",
        "expires_at": "2099-01-01T00:00:00Z",
        "cnf": {"jkt": key.jkt},
        "scope": ["usp_bookings_get"],
    })
    assert client._guard_scope(narrow, "usp_bookings_get") is narrow
    assert client._guard_scope(narrow, "usp_bookings_cancel") is None


# ---------------------------------------------------------------------------
# Issuance-side checks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_creation_call_carries_a_proof_and_no_authorization(monkeypatch):
    key = PopKey.generate()
    client, mock = _client(monkeypatch, key=key)
    await _book(client)

    create = mock.requests[-1]
    assert "Authorization" not in create.headers
    assert create.headers["DPoP"]
    assert create.headers["USP-Agent"].startswith('profile="')
    assert create.headers["Idempotency-Key"]

    header = json.loads(
        base64.urlsafe_b64decode(
            create.headers["DPoP"].split(".")[0] + "=="
        )
    )
    assert header["typ"] == "dpop+jwt"
    assert header["alg"] == "EdDSA"
    assert "d" not in header["jwk"], "the private key must never leave the process"

    claims = _claims(create.headers["DPoP"])
    assert "ath" not in claims, "no ath at issuance -- there is no credential yet"
    assert claims["htm"] == "POST"
    assert claims["htu"] == f"{ENDPOINT}/bookings"


@pytest.mark.asyncio
async def test_issued_credential_is_verified_against_our_own_key(monkeypatch):
    """A business that binds the credential to someone else's key is rejected."""
    key, other = PopKey.generate(), PopKey.generate()
    mock = MockPopBusinessTransport()
    mock.patch(monkeypatch)
    # Business issues a credential bound to a key we do not hold.
    original_issue = mock.business.issue
    mock.business.issue = lambda rid, *, dpop, **kw: {
        **original_issue(rid, dpop=dpop, **kw), "cnf": {"jkt": other.jkt},
    }
    client = USPBusinessRestClient(
        business_profile(), pop_key=key, authenticate=True,
    )
    with pytest.raises(CredentialBindingError, match="not this agent's key"):
        await client.create_booking(
            service_id="svc_1", slot_id="slot_1", buyer={"name": "A", "email": "a@b.c"},
        )


@pytest.mark.asyncio
async def test_unbound_credential_is_refused_under_pop(monkeypatch):
    """A credential returned without cnf MUST NOT be treated as sender-constrained."""
    key = PopKey.generate()
    mock = MockPopBusinessTransport()
    mock.patch(monkeypatch)
    original_issue = mock.business.issue
    mock.business.issue = lambda rid, *, dpop, **kw: {
        k: v for k, v in original_issue(rid, dpop=dpop, **kw).items() if k != "cnf"
    }
    client = USPBusinessRestClient(
        business_profile(), pop_key=key, authenticate=True,
    )
    with pytest.raises(CredentialBindingError, match="no 'cnf'"):
        await client.create_booking(
            service_id="svc_1", slot_id="slot_1", buyer={"name": "A", "email": "a@b.c"},
        )


@pytest.mark.asyncio
async def test_presentation_binds_the_proof_to_the_credential(monkeypatch):
    key = PopKey.generate()
    client, mock = _client(monkeypatch, key=key)
    booking_id, credential = await _book(client)
    await client.cancel_booking(booking_id, credential=credential)

    cancel = mock.requests[-1]
    assert cancel.headers["Authorization"] == f"DPoP {credential.value}"
    claims = _claims(cancel.headers["DPoP"])
    assert claims["ath"] == credential_ath(credential.value)
    assert claims["htm"] == "POST"


@pytest.mark.asyncio
async def test_nonce_challenge_is_answered_once(monkeypatch):
    """A business MAY demand a nonce; the client MUST support being challenged."""
    key = PopKey.generate()
    mock = MockPopBusinessTransport(PopBusiness(audience=PROFILE_URI, require_nonce=True))
    client, mock = _client(monkeypatch, key=key, mock=mock)

    booking = await client.create_booking(
        service_id="svc_1", slot_id="slot_1", buyer={"name": "A", "email": "a@b.c"},
    )
    assert booking.id
    # One challenge, one retry carrying the nonce.
    assert _claims(mock.requests[-1].headers["DPoP"]).get("nonce")
    assert len(mock.requests) == 2


def _patch_transport(monkeypatch, handler):
    """Replace traced_async_client with a MockTransport using *handler*."""

    def fake_traced(**kwargs):
        merged = dict(kwargs)
        merged.setdefault("follow_redirects", True)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **merged)

    monkeypatch.setattr("booking_agent.rest_client.traced_async_client", fake_traced)


@pytest.mark.asyncio
async def test_header_only_dpop_nonce_challenge_is_answered(monkeypatch):
    """Spec-literal REST: ``DPoP-Nonce`` on 401 without JSON ``data.nonce``."""
    key = PopKey.generate()
    base = MockPopBusinessTransport(PopBusiness(audience=PROFILE_URI))
    booking_posts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal booking_posts
        if request.method == "POST" and request.url.path.endswith("/bookings"):
            booking_posts += 1
            if booking_posts == 1:
                return httpx.Response(
                    401,
                    json={"error": {"code": "pop_proof_invalid", "content": "need nonce"}},
                    headers={
                        "DPoP-Nonce": "hdr-only-nonce",
                        "WWW-Authenticate": 'DPoP realm="usp"',
                    },
                )
        return base.handle(request)

    _patch_transport(monkeypatch, handle)
    client = USPBusinessRestClient(
        business_profile(),
        agent_profile_url=AGENT_PROFILE,
        pop_key=key,
        authenticate=True,
    )
    booking = await client.create_booking(
        service_id="svc_1", slot_id="slot_1", buyer={"name": "A", "email": "a@b.c"},
    )
    assert booking.id
    assert _claims(base.requests[-1].headers["DPoP"])["nonce"] == "hdr-only-nonce"
    assert booking_posts == 2


@pytest.mark.asyncio
async def test_dpop_nonce_header_wins_over_json_body_on_401(monkeypatch):
    key = PopKey.generate()
    base = MockPopBusinessTransport(PopBusiness(audience=PROFILE_URI))
    booking_posts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal booking_posts
        if request.method == "POST" and request.url.path.endswith("/bookings"):
            booking_posts += 1
            if booking_posts == 1:
                return httpx.Response(
                    401,
                    json={
                        "error": {
                            "code": "proof_nonce_required",
                            "content": "nonce required",
                            "data": {"nonce": "body-nonce"},
                        },
                    },
                    headers={
                        "DPoP-Nonce": "header-nonce",
                        "WWW-Authenticate": 'DPoP realm="usp"',
                    },
                )
        return base.handle(request)

    _patch_transport(monkeypatch, handle)
    client = USPBusinessRestClient(
        business_profile(),
        agent_profile_url=AGENT_PROFILE,
        pop_key=key,
        authenticate=True,
    )
    await client.create_booking(
        service_id="svc_1", slot_id="slot_1", buyer={"name": "A", "email": "a@b.c"},
    )
    assert _claims(base.requests[-1].headers["DPoP"])["nonce"] == "header-nonce"


@pytest.mark.asyncio
async def test_empty_dpop_nonce_header_does_not_clear_stored_nonce(monkeypatch):
    key = PopKey.generate()
    base = MockPopBusinessTransport(PopBusiness(audience=PROFILE_URI))

    def handle(request: httpx.Request) -> httpx.Response:
        response = base.handle(request)
        if (
            request.method == "POST"
            and request.url.path.endswith("/bookings")
            and response.status_code == 201
        ):
            return httpx.Response(
                201,
                json=response.json(),
                headers={"DPoP-Nonce": "keep-me"},
            )
        if request.method == "POST" and "/bookings/" in request.url.path and request.url.path.endswith("/cancel"):
            return httpx.Response(
                401,
                json={"error": {"code": "pop_proof_missing", "content": "missing"}},
                headers={"DPoP-Nonce": "", "WWW-Authenticate": 'DPoP realm="usp"'},
            )
        return response

    _patch_transport(monkeypatch, handle)
    client = USPBusinessRestClient(
        business_profile(),
        agent_profile_url=AGENT_PROFILE,
        pop_key=key,
        authenticate=True,
    )
    booking_id, credential = await _book(client)
    assert client._dpop_nonce == "keep-me"

    with pytest.raises(PopAuthorizationError):
        await client.cancel_booking(booking_id, credential=credential)
    assert client._dpop_nonce == "keep-me"


@pytest.mark.asyncio
async def test_success_response_dpop_nonce_is_sent_on_next_proof(monkeypatch):
    key = PopKey.generate()
    base = MockPopBusinessTransport(PopBusiness(audience=PROFILE_URI))

    def handle(request: httpx.Request) -> httpx.Response:
        response = base.handle(request)
        if (
            request.method == "POST"
            and request.url.path.endswith("/bookings")
            and not request.url.path.endswith("/cancel")
            and response.status_code == 201
        ):
            return httpx.Response(
                201,
                json=response.json(),
                headers={"DPoP-Nonce": "rotated-nonce"},
            )
        return response

    _patch_transport(monkeypatch, handle)
    client = USPBusinessRestClient(
        business_profile(),
        agent_profile_url=AGENT_PROFILE,
        pop_key=key,
        authenticate=True,
    )
    booking_id, credential = await _book(client)
    await client.cancel_booking(booking_id, credential=credential)
    cancel_proof = base.requests[-1].headers["DPoP"]
    assert _claims(cancel_proof)["nonce"] == "rotated-nonce"
    assert len(base.requests) == 2


def _claims(proof: str) -> dict:
    segment = proof.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


# ---------------------------------------------------------------------------
# Fail-closed, but only on the operations that must fail closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_policyless_business_stays_browsable_but_unbookable(monkeypatch):
    """Under the default `required` posture, a business publishing no usable
    authorization policy must still serve its public tier.

    Refusing at client construction would block catalog and availability too,
    which USP explicitly permits to be anonymous -- and would leave the agent
    unable to even show the user what it cannot book.
    """
    from booking_agent.authorization import (
        AuthorizationRefused,
        ScopedCredentialPosture,
        evaluate,
    )

    mock = MockPopBusinessTransport().patch(monkeypatch)
    profile = business_profile(with_policy=False)
    decision = evaluate(
        profile.authorization,
        posture=ScopedCredentialPosture.REQUIRED,
        exposes_privileged_capabilities=profile.exposes_privileged_capabilities,
        defer=True,
    )
    client = USPBusinessRestClient(
        profile, agent_profile_url=AGENT_PROFILE, decision=decision,
    )

    # Public: allowed, and anonymous.
    await client.list_services()
    assert "DPoP" not in mock.requests[-1].headers

    # Privileged: refused before anything reaches the wire.
    before = len(mock.requests)
    with pytest.raises(AuthorizationRefused) as caught:
        await client.create_booking(
            service_id="svc_1", slot_id="slot_1", buyer={"name": "A", "email": "a@b.c"},
        )
    assert caught.value.code == "authorization_policy_absent"
    assert len(mock.requests) == before, "the refused call must not be sent"
