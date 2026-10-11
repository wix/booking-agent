"""A business-side ``platform_key_pop`` verifier, for tests.

No business implements ``platform_key_pop`` today, so this stands in as the only
thing the client can interoperate with.  It implements the normative
verification order from USP Section 10.1.6 exactly, in order, because the order is
where the security lives -- most obviously at step 6, where comparing the
proof key's thumbprint against the recorded ``cnf.jkt`` is the *only* check that
rejects a credential replayed with a different key.  A verifier that checks the
signature and stops accepts that attack, and the signature does verify: it is
signed correctly, just by the wrong key.

This is deliberately one implementation shared by the ``httpx.MockTransport``
unit-test harness and the HTTP offline stack, so the two cannot drift.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

#: USP Section 10.1.6: reject a proof whose ``iat`` falls outside this window, and
#: cache ``jti`` for at least twice it.
SKEW_SECONDS = 60
JTI_CACHE_SECONDS = 2 * SKEW_SECONDS


class ProofRejected(Exception):
    """A verification failure, carrying the USP Section 10.1.1 error code."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        nonce: Optional[str] = None,
        nonce_header_only: bool = False,
    ):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.nonce = nonce
        self.nonce_header_only = nonce_header_only

    def to_response(self) -> Tuple[int, Dict[str, Any]]:
        error: Dict[str, Any] = {"code": self.code, "content": self.message}
        if self.nonce and not self.nonce_header_only:
            error["data"] = {"nonce": self.nonce}
        return 401, {"error": error}


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _jkt(jwk: Dict[str, str]) -> str:
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(
        hashlib.sha256(canonical.encode()).digest()
    ).decode().rstrip("=")


@dataclass
class IssuedCredential:
    value: str
    jkt: str
    resource_id: str
    expires_at: datetime


@dataclass
class PopBusiness:
    """In-memory business state: issued credentials, seen ``jti``s, key bindings."""

    #: Business profile URIs this deployment is reachable at.
    audience: str = "https://mock-business.example/.well-known/usp"
    #: When set, the first proof presented is challenged for a nonce.
    require_nonce: bool = False

    credentials: Dict[str, IssuedCredential] = field(default_factory=dict)
    seen_jti: Dict[str, float] = field(default_factory=dict)
    #: Trust-on-first-use binding of a key thumbprint to the agent profile URI.
    jkt_by_profile: Dict[str, str] = field(default_factory=dict)
    issued_nonces: Set[str] = field(default_factory=set)
    _challenged: Set[str] = field(default_factory=set)

    # -- Verification ------------------------------------------------------

    def verify(
        self,
        *,
        method: str,
        url: str,
        authorization: Optional[str],
        dpop: Optional[str],
        agent_profile: Optional[str] = None,
        resource_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Optional[IssuedCredential]:
        """Run the Section 10.1.6 verification order. Returns the credential, if any.

        Raises ``ProofRejected`` with the specific error code on any failure.
        """
        now = time.time() if now is None else now
        self._expire_jti(now)

        credential: Optional[IssuedCredential] = None
        presented_value: Optional[str] = None

        if authorization:
            scheme, _, value = authorization.partition(" ")
            presented_value = value.strip()
            if scheme.lower() == "bearer" and presented_value in self.credentials:
                # Anti-downgrade (Section 10.1.6): a cnf-bearing credential MUST NOT be
                # accepted as a bearer token, whatever mechanism the caller
                # declares.  Without this the whole scheme is bypassable by
                # changing one field.
                raise ProofRejected(
                    "pop_proof_missing",
                    "sender-constrained credential presented as a bearer token",
                )

            # 1. credential known and unexpired
            credential = self.credentials.get(presented_value)
            if credential is None:
                raise ProofRejected("pop_proof_invalid", "unknown credential")
            if credential.expires_at <= datetime.now(timezone.utc):
                raise ProofRejected("pop_proof_invalid", "credential expired")

            # 2. cnf present => proof mandatory, regardless of declared mechanism
            if not dpop:
                raise ProofRejected(
                    "pop_proof_missing",
                    "credential carries cnf but no proof was presented",
                )

            # 3. credential scope matches the addressed resource
            if resource_id is not None and credential.resource_id != resource_id:
                raise ProofRejected(
                    "pop_proof_invalid",
                    f"credential is scoped to {credential.resource_id}, not {resource_id}",
                )

        if not dpop:
            raise ProofRejected("pop_proof_missing", "no proof presented")

        header, claims, jwk = self._parse_and_verify_signature(dpop)

        # 6. thumbprint of the header JWK vs the recorded cnf.jkt (kid ignored).
        #    THE load-bearing step: everything above passes for a credential
        #    replayed with an attacker's key, because that proof is validly
        #    signed -- by the wrong key.
        thumbprint = _jkt(jwk)
        if credential is not None and thumbprint != credential.jkt:
            raise ProofRejected(
                "pop_key_mismatch",
                "proof key thumbprint does not match the key the credential is bound to",
            )

        # 8. htm / htu
        if claims.get("htm", "").upper() != method.upper():
            raise ProofRejected("pop_proof_invalid", "htm does not match the request method")
        if claims.get("htu") != _bare_url(url):
            raise ProofRejected("pop_proof_invalid", "htu does not match the request URI")

        # 10. ath binds the proof to the credential presented
        if credential is not None:
            expected_ath = base64.urlsafe_b64encode(
                hashlib.sha256(credential.value.encode()).digest()
            ).decode().rstrip("=")
            if claims.get("ath") != expected_ath:
                raise ProofRejected("pop_proof_invalid", "ath does not match the credential")
        elif "ath" in claims:
            raise ProofRejected("pop_proof_invalid", "ath present at issuance")

        # 11. iat within skew, and the nonce if one was demanded
        iat = claims.get("iat")
        if not isinstance(iat, int) or abs(now - iat) > SKEW_SECONDS:
            raise ProofRejected("pop_proof_invalid", "iat outside the ±60s window")
        self._check_nonce(thumbprint, claims)

        # 12. jti unseen, then recorded
        jti = claims.get("jti")
        if not isinstance(jti, str) or not jti:
            raise ProofRejected("pop_proof_invalid", "missing jti")
        if jti in self.seen_jti:
            raise ProofRejected("pop_proof_replayed", "this proof was already presented")
        self.seen_jti[jti] = now

        if agent_profile:
            # Trust-on-first-use: the key is the authenticated principal; the
            # profile URI is self-asserted metadata, so it is recorded, not trusted.
            self.jkt_by_profile.setdefault(agent_profile, thumbprint)

        return credential

    def _parse_and_verify_signature(self, proof: str):
        parts = proof.split(".")
        if len(parts) != 3:
            raise ProofRejected("pop_proof_invalid", "proof is not a compact JWS")
        raw_header, raw_claims, raw_signature = parts
        try:
            header = json.loads(_b64url_decode(raw_header))
            claims = json.loads(_b64url_decode(raw_claims))
        except Exception:
            raise ProofRejected("pop_proof_invalid", "proof is not valid JSON") from None

        # 4. typ matches the binding
        if header.get("typ") != "dpop+jwt":
            raise ProofRejected("pop_proof_invalid", "typ is not dpop+jwt")
        # 5. alg accepted and not 'none'
        if header.get("alg") != "EdDSA":
            raise ProofRejected("pop_proof_invalid", "alg is not EdDSA")
        jwk = header.get("jwk")
        if not isinstance(jwk, dict) or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            raise ProofRejected("pop_proof_invalid", "proof header carries no usable JWK")
        if "d" in jwk:
            raise ProofRejected("pop_proof_invalid", "proof header JWK carries a private key")

        # 7. signature verifies against that header JWK
        try:
            public = Ed25519PublicKey.from_public_bytes(_b64url_decode(jwk["x"]))
            public.verify(
                _b64url_decode(raw_signature),
                f"{raw_header}.{raw_claims}".encode("ascii"),
            )
        except (InvalidSignature, ValueError):
            raise ProofRejected("pop_proof_invalid", "signature does not verify") from None

        return header, claims, jwk

    def _check_nonce(self, thumbprint: str, claims: Dict[str, Any]) -> None:
        if not self.require_nonce:
            return
        nonce = claims.get("nonce")
        if nonce and nonce in self.issued_nonces:
            return
        if thumbprint in self._challenged and nonce:
            raise ProofRejected("pop_proof_invalid", "unrecognized nonce")
        self._challenged.add(thumbprint)
        fresh = secrets.token_urlsafe(16)
        self.issued_nonces.add(fresh)
        raise ProofRejected("proof_nonce_required", "a proof nonce is required", nonce=fresh)

    def _expire_jti(self, now: float) -> None:
        for jti, seen in list(self.seen_jti.items()):
            if now - seen > JTI_CACHE_SECONDS:
                del self.seen_jti[jti]

    # -- Issuance ----------------------------------------------------------

    def issue(
        self, resource_id: str, *, dpop: str, ttl_hours: int = 24 * 90,
    ) -> Dict[str, Any]:
        """Issue a credential bound to the thumbprint of the presenting key.

        Binding happens here, in band, from the proof that was just verified --
        there is no registration step and nothing to look up.
        """
        header = json.loads(_b64url_decode(dpop.split(".")[0]))
        thumbprint = _jkt(header["jwk"])
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        value = f"bsc_{secrets.token_hex(16)}"
        self.credentials[value] = IssuedCredential(
            value=value,
            jkt=thumbprint,
            resource_id=resource_id,
            expires_at=expires_at,
        )
        return {
            "value": value,
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            # Echoing cnf is what lets the agent detect a business that accepted
            # the proof and then issued an unbound credential anyway.
            "cnf": {"jkt": thumbprint},
        }

    def invalidate(self, value: str) -> None:
        """Terminal state invalidates the credential; USP defines no revocation call."""
        self.credentials.pop(value, None)


def _bare_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
