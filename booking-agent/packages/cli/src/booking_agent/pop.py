"""``platform_key_pop`` -- proof-of-possession keys and proofs (USP Section 10.1.6).

The agent holds one Ed25519 keypair per business endpoint.  The private half is
never transmitted; the public half travels only inside the JOSE header of each
proof.  The business computes the RFC 7638 thumbprint of that key and binds the
issued ``booking_scoped_credential`` to it as ``cnf.jkt``, so a credential value
that leaks -- via a log, a trace, a TLS-terminating proxy, or a database
compromise -- is useless without the key.

What this module is deliberately *not*:

* It is not RFC 9421 request signing.  No ``Content-Digest``, no signature base,
  no covered components.
* It publishes nothing.  The platform profile stays keyless and shared; there
  are no ``keys``/``signing_keys``, no rotation, and no per-instance profiles.
* The key is not a platform identity.  It is per-endpoint and self-generated, so
  it identifies "the holder of this credential", nothing more.

"Ephemeral" here means *never published and never a platform identity* -- not
short-lived.  The key must outlive every credential bound to it, or those
credentials become unusable; see ``PopKeyStore.release``.

Only the REST binding is implemented.  The MCP binding (``typ: usp-pop+jwt``
with ``usp_m``/``aud``/``usp_p`` over JCS-canonicalized params) is specified but
has no client anywhere -- see issue #199.  REST proofs carry the latest
``DPoP-Nonce`` from any HTTP response; MCP, when built, must use
``nonce_from_pop_error_body`` from ``authorization``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import settings
from .authorization import nonce_from_pop_error_body
from .secure_file import ensure_private_dir, write_private_text

logger = logging.getLogger(__name__)

_KEYSTORE_FILE_NAME = "pop_keys.json"
_STORE_VERSION = 1

#: The only algorithm this client emits.  USP Section 10.1.6 makes EdDSA (Ed25519) the
#: floor that any business advertising ``platform_key_pop`` MUST support, chosen
#: to avoid the ECDSA fixed-width ``r||s`` vs DER interoperability trap.
ALG = "EdDSA"

#: REST proofs are standard RFC 9449 DPoP proofs.  The MCP binding uses
#: ``usp-pop+jwt`` precisely so neither proof can be replayed as the other.
REST_PROOF_TYP = "dpop+jwt"

MECHANISM = "platform_key_pop"


def b64url(raw: bytes) -> str:
    """base64url without padding, as every JOSE and RFC 7638 value is encoded."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def compute_jkt(public_jwk: Dict[str, str]) -> str:
    """RFC 7638 JWK SHA-256 thumbprint of an OKP/Ed25519 public key.

    The hash input is the *required* members only, lexicographically ordered
    (``crv``, ``kty``, ``x``), serialized with no whitespace.  Any other member --
    ``alg``, ``kid``, ``use`` -- must be excluded or the thumbprint will not
    match what the business computes.
    """
    required = {
        "crv": public_jwk["crv"],
        "kty": public_jwk["kty"],
        "x": public_jwk["x"],
    }
    canonical = json.dumps(required, separators=(",", ":"), sort_keys=True)
    jkt = b64url(hashlib.sha256(canonical.encode("utf-8")).digest())
    # schemas/usp.json#/$defs/ConfirmationKey pins this to exactly 43 chars.
    if len(jkt) != 43:
        raise ValueError(f"computed jkt has length {len(jkt)}, expected 43")
    return jkt


def credential_ath(credential_value: str) -> str:
    """RFC 9449 ``ath``: base64url(SHA-256(credential value)).

    Binds a proof to the specific credential presented, so a proof captured
    alongside one credential cannot be paired with another.
    """
    return b64url(hashlib.sha256(credential_value.encode("utf-8")).digest())


def normalize_htu(url: str) -> str:
    """RFC 9449 ``htu``: the request URI without query or fragment."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


@dataclass(frozen=True)
class PopKey:
    """An Ed25519 keypair and its derived public JWK and thumbprint."""

    private_key: Ed25519PrivateKey
    public_jwk: Dict[str, str]
    jkt: str

    @classmethod
    def generate(cls) -> "PopKey":
        return cls.from_private_bytes(Ed25519PrivateKey.generate())

    @classmethod
    def from_private_bytes(cls, private_key: Ed25519PrivateKey) -> "PopKey":
        public_jwk = _public_jwk(private_key.public_key())
        return cls(
            private_key=private_key,
            public_jwk=public_jwk,
            jkt=compute_jkt(public_jwk),
        )

    @classmethod
    def from_seed_b64(cls, seed_b64: str) -> "PopKey":
        raw = base64.urlsafe_b64decode(seed_b64 + "=" * (-len(seed_b64) % 4))
        return cls.from_private_bytes(Ed25519PrivateKey.from_private_bytes(raw))

    def seed_b64(self) -> str:
        raw = self.private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return b64url(raw)

    def sign(self, message: bytes) -> bytes:
        return self.private_key.sign(message)

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let the private key reach a log line or a traceback.
        return f"PopKey(jkt={self.jkt!r})"


def _public_jwk(public_key: Ed25519PublicKey) -> Dict[str, str]:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {"kty": "OKP", "crv": "Ed25519", "x": b64url(raw)}


def build_rest_proof(
    key: PopKey,
    *,
    htm: str,
    htu: str,
    ath: Optional[str] = None,
    nonce: Optional[str] = None,
    iat: Optional[int] = None,
    jti: Optional[str] = None,
) -> str:
    """Build an RFC 9449 ``dpop+jwt`` proof as a compact JWS.

    ``ath`` is absent at issuance (there is no credential yet) and REQUIRED at
    presentation.  ``jti`` is fresh per *attempt*, not per logical call -- a
    retried request must carry a new one or the business's replay cache will
    reject it as ``pop_proof_replayed``.

    Hand-rolled rather than pulled from a JOSE library: the header is fixed, the
    claim set is six fields, and a library would add a dependency whose
    algorithm-negotiation defaults would need auditing to guarantee we never
    emit ``alg: none``.
    """
    header = {
        "typ": REST_PROOF_TYP,
        "alg": ALG,
        # The public JWK travels here and nowhere else.  It carries no private
        # member ('d'), which a business MUST reject.
        "jwk": key.public_jwk,
    }
    claims: Dict[str, Any] = {
        "htm": htm.upper(),
        "htu": normalize_htu(htu),
        "iat": int(time.time()) if iat is None else iat,
        "jti": jti or str(uuid.uuid4()),
    }
    if ath is not None:
        claims["ath"] = ath
    if nonce is not None:
        claims["nonce"] = nonce

    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    )
    signature = b64url(key.sign(signing_input.encode("ascii")))
    return f"{signing_input}.{signature}"


def usp_error_payload(response: httpx.Response) -> Dict[str, Any]:
    """Return the ``error`` object of a USP error body, or the body itself."""
    try:
        body = response.json()
    except Exception:
        return {}
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    return error if isinstance(error, dict) else body


def dpop_nonce_from_response(response: httpx.Response) -> Optional[str]:
    """Return a non-empty ``DPoP-Nonce`` response header, if present.

    Any response may rotate the nonce; an empty or missing header does not clear
    the value already held.
    """
    nonce = response.headers.get("DPoP-Nonce")
    return nonce if isinstance(nonce, str) and nonce else None


def nonce_challenge_from_401(response: httpx.Response) -> Optional[str]:
    """Return the nonce a business is challenging for on HTTP 401.

    REST (RFC 9449 / USP Section 10.1.6): ``DPoP-Nonce`` header first.  JSON
    ``proof_nonce_required`` / ``data.nonce`` is the MCP channel and REST
    fallback when the header is absent.
    """
    header_nonce = dpop_nonce_from_response(response)
    if header_nonce is not None:
        return header_nonce
    return nonce_from_pop_error_body(usp_error_payload(response))


def keystore_path() -> Path:
    base = settings.booking_agent_home()
    ensure_private_dir(base)
    return base / _KEYSTORE_FILE_NAME


def endpoint_key(endpoint: str) -> str:
    """Normalize a REST endpoint to the origin used to key the store.

    Matches ``USPTokenStore._base_url`` so a business is one principal
    regardless of which path the client happened to resolve.
    """
    parsed = urlsplit(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


class PopKeyStore:
    """Per-business-endpoint Ed25519 keys, persisted 0600.

    The key is per *endpoint*; credentials are per *resource*.  That asymmetry is
    the reason ``release`` is refcounted rather than a plain delete -- see its
    docstring.
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = path or keystore_path()
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to read PoP keystore at %s", self._path, exc_info=True)
            return
        if not isinstance(data, dict) or data.get("version") != _STORE_VERSION:
            return
        entries = data.get("keys")
        if isinstance(entries, dict):
            self._entries = entries

    def _save(self) -> None:
        payload = {"version": _STORE_VERSION, "keys": self._entries}
        try:
            write_private_text(self._path, json.dumps(payload, indent=2))
        except OSError:
            logger.warning("Failed to persist PoP keystore to %s", self._path, exc_info=True)

    def get_or_create(self, endpoint: str) -> PopKey:
        """Return the key for *endpoint*, generating one on first contact.

        This is the whole of platform onboarding under ``platform_key_pop``:
        local, with no network call and no registration round trip.
        """
        slot = endpoint_key(endpoint)
        entry = self._entries.get(slot)
        if entry and entry.get("seed"):
            try:
                return PopKey.from_seed_b64(entry["seed"])
            except Exception:
                logger.warning(
                    "Unreadable PoP key for %s; regenerating. Credentials bound to "
                    "the old key can no longer be used.", slot, exc_info=True,
                )
        key = PopKey.generate()
        self._entries[slot] = {"seed": key.seed_b64(), "jkt": key.jkt}
        self._save()
        return key

    def peek(self, endpoint: str) -> Optional[PopKey]:
        """Return the existing key for *endpoint*, or None. Never generates."""
        entry = self._entries.get(endpoint_key(endpoint))
        if not entry or not entry.get("seed"):
            return None
        try:
            return PopKey.from_seed_b64(entry["seed"])
        except Exception:
            return None

    def release(self, endpoint: str, *, still_in_use: bool) -> bool:
        """Drop the key for *endpoint* once nothing is bound to it any more.

        Called when a resource reaches a terminal state.  *still_in_use* must be
        True if any other non-expired credential issued by that business remains
        in the session store.

        Destroying the key unconditionally would be a data-loss bug rather than
        hygiene: with two live bookings at one business, cancelling the first
        would permanently orphan the second, whose credential is bound to a
        ``jkt`` whose private half no longer exists.  USP defines no re-issuance
        path for that, so the booking could never be cancelled from the agent
        again.

        Returns True if the key was removed.
        """
        slot = endpoint_key(endpoint)
        if still_in_use or slot not in self._entries:
            return False
        del self._entries[slot]
        self._save()
        return True
