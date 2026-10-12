"""``AuthorizationPolicy`` parsing and mechanism selection (USP Section 10.1.6).

USP splits operations into two tiers.  *Public* operations -- catalog,
availability, profile discovery -- MAY remain anonymous.  *Privileged*
operations -- creating, updating, confirming, cancelling or rescheduling a
booking, holds, waitlist mutations, payment-adjacent completion, registry
writes, and any response carrying buyer personal data -- MUST be authenticated.
A resource identifier is not a credential.

The *mechanism* is business-declared: the business publishes an
``AuthorizationPolicy`` and the platform selects one it supports.  Two rules
make that safe, and both are easy to get backwards:

* Unrecognized mechanisms are **ignored, never treated as accepted**.  A client
  that shrugged and continued anonymously against a policy it did not
  understand would defeat the whole tier split.
* Where the policy is missing or nothing is mutually supported, the client
  **fails closed** rather than falling back to anonymous.

Placement differs by deployment mode:

* **Standalone** -- top-level ``authorization`` in ``/.well-known/usp``.
* **UCP-Native** -- ``config.authorization`` on the ``dev.usp-protocol.services``
  binding.  A binding carrying ``authorization`` as a *direct member* is
  malformed: ``schemas/usp.json`` gives it a never-satisfiable schema so that it
  is rejected rather than silently accepted, which would let a business believe
  it had published a policy no conforming consumer reads.  We therefore treat
  that shape as *no policy published* and fail closed -- we do not read it.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from . import settings

logger = logging.getLogger(__name__)

#: USP Section 10.1.6 mechanism identifiers this client can actually satisfy.
#: ``platform_key_pop`` is the only one implemented; the rest are recognized so
#: that diagnostics can say *why* a business is unreachable.
MECHANISM_PLATFORM_KEY_POP = "platform_key_pop"
MECHANISM_BOOKING_SCOPED_CREDENTIAL = "booking_scoped_credential"
MECHANISM_OAUTH2_BEARER = "oauth2_bearer"
MECHANISM_API_KEY = "api_key"
MECHANISM_HTTP_MESSAGE_SIGNATURE = "http_message_signature"
MECHANISM_MTLS = "mtls"

#: Ordered by preference.  Only the first is implemented in this version;
#: ``oauth2_bearer`` support is dormant (see ``oauth_dormant``) and gated on #199.
SUPPORTED_MECHANISMS: tuple[str, ...] = (MECHANISM_PLATFORM_KEY_POP,)


class AuthorizationError(Exception):
    """Base class for fail-closed authorization outcomes."""


#: USP Section 10.1.1 error codes relating to proofs.  Note the ``pop_`` prefix: an
#: earlier draft of the client checklist called the replay code ``proof_replayed``,
#: which matches nothing on the wire.
ERR_POP_PROOF_MISSING = "pop_proof_missing"
ERR_POP_PROOF_INVALID = "pop_proof_invalid"
ERR_POP_KEY_MISMATCH = "pop_key_mismatch"
ERR_POP_PROOF_REPLAYED = "pop_proof_replayed"
ERR_PROOF_NONCE_REQUIRED = "proof_nonce_required"

#: All 401, and all terminal for the caller except the nonce challenge, which is
#: a single-round-trip negotiation rather than a failure.
POP_ERROR_CODES = frozenset({
    ERR_POP_PROOF_MISSING,
    ERR_POP_PROOF_INVALID,
    ERR_POP_KEY_MISMATCH,
    ERR_POP_PROOF_REPLAYED,
    ERR_PROOF_NONCE_REQUIRED,
})


def nonce_from_pop_error_body(payload: Dict[str, Any]) -> Optional[str]:
    """Return a proof nonce from a USP error object (MCP and REST 401 fallback).

    MCP has no HTTP headers, so the MCP binding carries the nonce in
    ``error.data.nonce`` when ``error.code`` is ``proof_nonce_required``.  REST
    prefers the ``DPoP-Nonce`` header; this parser is the fallback when the
    header is absent on a 401.
    """
    if payload.get("code") != ERR_PROOF_NONCE_REQUIRED:
        return None
    data = payload.get("data")
    nonce = data.get("nonce") if isinstance(data, dict) else None
    return nonce if isinstance(nonce, str) and nonce else None


class AuthorizationRefused(AuthorizationError):
    """The client refuses to perform a privileged operation against this business."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthorizationPolicy(BaseModel):
    """``schemas/profile.json#/$defs/AuthorizationPolicy``.

    Both members are REQUIRED by the schema.  The object is deliberately open
    (``additionalProperties: true``) -- accepted proof algorithms and similar
    extensions are advertised through it -- so unknown members are preserved
    rather than dropped.
    """

    model_config = ConfigDict(extra="allow")

    privileged_operations_require_authentication: bool
    accepted_mechanisms: List[str]
    token_endpoint: Optional[str] = None

    def select_mechanism(self) -> Optional[str]:
        """Return the best mutually supported mechanism, or None.

        Unrecognized identifiers in ``accepted_mechanisms`` are skipped, never
        assumed acceptable.
        """
        for candidate in SUPPORTED_MECHANISMS:
            if candidate in self.accepted_mechanisms:
                return candidate
        return None


def parse_policy(raw: Any) -> Optional[AuthorizationPolicy]:
    """Parse an ``authorization`` object, or return None when absent/malformed.

    A malformed policy is reported as *absent* rather than raising, so the
    caller reaches the same fail-closed path as a business that published
    nothing -- the distinction matters for the log line, not for the decision.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        logger.warning("Ignoring non-object authorization policy of type %s", type(raw).__name__)
        return None
    try:
        return AuthorizationPolicy.model_validate(raw)
    except Exception:
        logger.warning("Ignoring malformed authorization policy: %r", raw, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# The issued credential
# ---------------------------------------------------------------------------

class ConfirmationKey(BaseModel):
    """``schemas/usp.json#/$defs/ConfirmationKey`` -- the RFC 7800 ``cnf`` claim.

    Restricted to the JWK-thumbprint form; the embedded-key and key-reference
    forms are not used by USP.  ``extra="forbid"`` is load-bearing: ignoring an
    unrecognized confirmation method would silently downgrade a
    sender-constrained credential to a bearer token, so the Section 10.1.6
    forward-compatibility rule deliberately does NOT extend to this object.
    """

    model_config = ConfigDict(extra="forbid")

    jkt: str

    @field_validator("jkt")
    @classmethod
    def _check_thumbprint(cls, v: str) -> str:
        if not _JKT_RE.fullmatch(v):
            raise ValueError(
                "jkt must be an unpadded base64url SHA-256 thumbprint of exactly 43 characters"
            )
        return v


class BookingScopedCredential(BaseModel):
    """``schemas/profile.json#/$defs/BookingScopedCredential``.

    Returned on a creation response only, never on reads, and authorizing
    continuation operations on exactly one booking or waitlist entry.  Also a
    **closed** object for the same reason as ``ConfirmationKey``.
    """

    model_config = ConfigDict(extra="forbid")

    value: str
    expires_at: datetime
    cnf: Optional[ConfirmationKey] = None
    #: USP method names this credential authorizes on its resource.  Absent means
    #: the full continuation set for the resource type -- never another resource.
    scope: Optional[List[str]] = None

    @field_validator("value")
    @classmethod
    def _check_value(cls, v: str) -> str:
        if not v.startswith("bsc_") or len(v) < 26:
            raise ValueError("credential value must start with 'bsc_' and be at least 26 characters")
        return v

    @property
    def is_sender_constrained(self) -> bool:
        return self.cnf is not None

    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= current

    def covers(self, usp_method: str) -> bool:
        return self.scope is None or usp_method in self.scope


_JKT_RE = re.compile(r"[A-Za-z0-9_-]{43}")


class CredentialBindingError(AuthorizationError):
    """The business issued a credential that is not bound to our key."""


def issued_credential_payload(data: Dict[str, Any]) -> Any:
    """Locate ``booking_scoped_credential`` on a creation response.

    Standalone USP returns it at the body root. UCP-Native paid bookings return
    it as a sibling of ``booking`` at the checkout root, or inside the
    ``dev.usp-protocol.services.paid_bookings`` extension. Creation is the only
    place the value appears.
    """
    from .namespace import CAP_PAID_BOOKINGS

    if not isinstance(data, dict):
        return None
    payload = data.get("booking_scoped_credential")
    if payload is not None:
        return payload
    extension = data.get(CAP_PAID_BOOKINGS)
    if isinstance(extension, dict):
        return extension.get("booking_scoped_credential")
    return None


def parse_issued_credential(
    payload: Any, *, own_jkt: str, required: bool,
) -> Optional[BookingScopedCredential]:
    """Parse and verify a ``booking_scoped_credential`` from a creation response.

    Verifies ``cnf.jkt`` against *own_jkt*: this detects a business that accepted
    the proof and then issued a credential bound to something else, or to
    nothing.  A credential returned without ``cnf`` MUST NOT be treated as
    sender-constrained -- when we authenticated with ``platform_key_pop``
    (*required*), that is a protocol violation rather than a soft downgrade,
    because continuing would hand a plain bearer secret to a caller that
    believes it holds a bound one.
    """
    if payload is None:
        return None
    try:
        credential = BookingScopedCredential.model_validate(payload)
    except Exception as exc:
        raise CredentialBindingError(
            f"business returned a malformed booking_scoped_credential: {exc}"
        ) from exc

    if credential.cnf is None:
        if required:
            raise CredentialBindingError(
                "business issued a booking_scoped_credential with no 'cnf' after a "
                "platform_key_pop request; refusing to treat an unbound credential as "
                "sender-constrained."
            )
        return credential

    if credential.cnf.jkt != own_jkt:
        raise CredentialBindingError(
            f"business bound the credential to jkt {credential.cnf.jkt!r}, which is not "
            f"this agent's key ({own_jkt!r}); the credential would be unusable."
        )
    return credential


# ---------------------------------------------------------------------------
# Deployment posture
# ---------------------------------------------------------------------------

class ScopedCredentialPosture(str, Enum):
    """How strictly to enforce the Section 10.1.6 privileged-operation floor.

    ``REQUIRED`` is both the conformant posture and the default.  USP is
    unambiguous that privileged operations MUST be authenticated and that a
    consumer fails closed rather than falling back to anonymous, so shipping a
    laxer default would be shipping a known deviation.

    v2 is the first production release, so there is no installed base whose
    flows would start refusing -- the usual argument for easing in a security
    default does not apply.  No business implements ``platform_key_pop`` yet
    (spec issues #157 / #162), which means privileged calls against a business
    that publishes no ``authorization`` policy now refuse until the server side
    ships.  That is the intended behaviour: an unauthenticated booking is
    exactly what the mechanism exists to prevent.

    ``PREFERRED`` remains available for deployments that need to transact against
    a not-yet-conformant business: it warns and proceeds instead of refusing.
    ``DISABLED`` is for the offline stack and mock-free local runs.
    """

    REQUIRED = "required"
    PREFERRED = "preferred"
    DISABLED = "disabled"


DEFAULT_POSTURE = ScopedCredentialPosture.REQUIRED

#: Precedence: explicit config -> environment -> default.
POSTURE_ENV_VAR = "BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL"


def resolve_posture(explicit: Optional[str] = None) -> ScopedCredentialPosture:
    raw = explicit or settings.get(POSTURE_ENV_VAR)
    if not raw:
        return DEFAULT_POSTURE
    try:
        return ScopedCredentialPosture(raw.strip().lower())
    except ValueError:
        logger.warning(
            "Unknown %s=%r; falling back to %s",
            POSTURE_ENV_VAR, raw, DEFAULT_POSTURE.value,
        )
        return DEFAULT_POSTURE


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

class AuthorizationDecision(BaseModel):
    """What the client will do for privileged operations against one business."""

    model_config = ConfigDict(frozen=True)

    mechanism: Optional[str] = None
    #: True when privileged requests should carry a proof.
    authenticate: bool = False
    #: Set when proceeding unauthenticated under a non-conformant posture.
    warning: Optional[str] = None
    #: Set when this business cannot be transacted with under the current
    #: posture.  Carried rather than raised so the refusal lands on the
    #: privileged call that is actually forbidden -- see ``raise_if_refused``.
    refusal_code: Optional[str] = None
    refusal_message: Optional[str] = None

    @property
    def uses_pop(self) -> bool:
        return self.mechanism == MECHANISM_PLATFORM_KEY_POP

    @property
    def refused(self) -> bool:
        return self.refusal_code is not None

    def raise_if_refused(self) -> None:
        """Enforce the refusal. Call this on privileged operations only.

        Public operations -- catalog, availability, profile discovery -- MAY
        remain unauthenticated under USP Section 10.1.6, so a business that publishes
        no usable policy is still browsable.  Refusing at client construction
        would block those too, which is both non-conformant and needlessly
        unhelpful: the agent could not even show the user what it cannot book.
        """
        if self.refusal_code is not None:
            raise AuthorizationRefused(self.refusal_code, self.refusal_message or "")


def evaluate(
    policy: Optional[AuthorizationPolicy],
    *,
    posture: ScopedCredentialPosture,
    exposes_privileged_capabilities: bool = True,
    business_label: str = "business",
    defer: bool = False,
) -> AuthorizationDecision:
    """Decide how to authenticate privileged operations against one business.

    Raises ``AuthorizationRefused`` on every fail-closed path, unless *defer* is
    set -- in which case the refusal is returned on the decision and enforced
    later by ``AuthorizationDecision.raise_if_refused``.  Callers that build a
    client used for public operations too must defer, or browsing a business
    would fail for want of an authorization policy it does not need.
    """
    if posture is ScopedCredentialPosture.DISABLED:
        # Offline stacks and mock-free local runs only.
        return AuthorizationDecision(mechanism=None, authenticate=False)

    if policy is None:
        if not exposes_privileged_capabilities:
            return AuthorizationDecision(mechanism=None, authenticate=False)
        return _refuse_or_warn(
            posture,
            defer=defer,
            code="authorization_policy_absent",
            message=(
                f"{business_label} exposes privileged capabilities but publishes no "
                f"authorization policy, so there is no mechanism to negotiate."
            ),
        )

    if not policy.privileged_operations_require_authentication:
        # Section 10.1.6: publishing false declares the deployment OUT OF CONFORMANCE
        # for privileged operations.  The platform MUST refuse to transact real
        # bookings or transmit buyer personal data -- refuse, not warn and continue.
        return _refuse_or_warn(
            posture,
            defer=defer,
            code="privileged_operations_unauthenticated",
            message=(
                f"{business_label} publishes "
                f"privileged_operations_require_authentication=false, declaring itself "
                f"out of conformance for privileged operations. Refusing to transact "
                f"real bookings or send buyer personal data."
            ),
        )

    mechanism = policy.select_mechanism()
    if mechanism is None:
        return _refuse_or_warn(
            posture,
            defer=defer,
            code="no_supported_mechanism",
            message=(
                f"{business_label} accepts {policy.accepted_mechanisms!r}, none of which "
                f"this client supports (supported: {list(SUPPORTED_MECHANISMS)!r})."
            ),
        )

    return AuthorizationDecision(mechanism=mechanism, authenticate=True)


def _refuse_or_warn(
    posture: ScopedCredentialPosture, *, code: str, message: str, defer: bool = False,
) -> AuthorizationDecision:
    if posture is ScopedCredentialPosture.REQUIRED:
        if defer:
            return AuthorizationDecision(
                mechanism=None,
                authenticate=False,
                refusal_code=code,
                refusal_message=message,
            )
        raise AuthorizationRefused(code, message)
    logger.warning(
        "%s Proceeding unauthenticated because %s=%s; this is not a conformant "
        "configuration and privileged calls may be rejected.",
        message, POSTURE_ENV_VAR, posture.value,
    )
    return AuthorizationDecision(mechanism=None, authenticate=False, warning=message)
