"""Async REST client and per-business OAuth token store for USP endpoints.

Wraps all USP REST API endpoints using httpx.AsyncClient. Constructed from a
resolved USPBusinessProfile, giving it access to the endpoint URL, declared
capabilities, and business metadata.

USPTokenStore manages per-business OAuth 2.0 credentials and bearer tokens
(USP spec Section 10.2.3). It supports:
  - Manual client credentials (onboarded out-of-band)
  - RFC 7591 Dynamic Client Registration
  - RFC 8414 OAuth Authorization Server Metadata discovery
  - OAuth 2.0 client_credentials grant for token acquisition

Designed for the REST binding now but structured so a future MCP or A2A
transport can implement the same method signatures.
"""
import json
import logging
import os
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse

import httpx

from booking_agent.http_retry import with_http_retry
from booking_agent.http_trace import traced_async_client

from . import usp_methods
from .authorization import (
    ERR_POP_KEY_MISMATCH,
    ERR_POP_PROOF_INVALID,
    ERR_POP_PROOF_MISSING,
    ERR_POP_PROOF_REPLAYED,
    POP_ERROR_CODES,
    AuthorizationDecision,
    BookingScopedCredential,
    issued_credential_payload,
    parse_issued_credential,
)
from .pop import (
    PopKey,
    build_rest_proof,
    credential_ath,
    dpop_nonce_from_response,
    nonce_challenge_from_401,
    usp_error_payload,
)
from .secure_file import write_private_text

from .models.wire import (
    AvailabilityResult,
    USPService,
    TimeSlot,
    Hold,
    Booking,
    USPPaymentResult,
    PostPaymentReturnRequest,
)
from .discovery import USPBusinessProfile
from .registry_url import registry_search_post_url
from .models.platform import (
    DesiredServiceTimePreference,
    ServiceSearchLocation,
    PriceRange,
    DurationRange,
    ServiceSearchResult,
    ServiceSearchPagination,
)

logger = logging.getLogger(__name__)


# Maps non-spec booking status values emitted by known non-compliant business
# servers to their nearest spec equivalent.  Entries here indicate a server-side
# compliance bug that should be fixed in the emitting implementation.
_NON_SPEC_STATUS_MAP: Dict[str, str] = {
    "created": "confirmed",
}


class BookingServerError(Exception):
    """Raised when a USP business server returns HTTP 200 with an error body.

    Per the REST spec, error conditions MUST use appropriate HTTP status codes
    (404 for not_found, 500/502 for upstream errors). Servers that return
    HTTP 200 for errors are non-compliant; this exception surfaces the
    server-provided error code so callers can act on it rather than receiving
    an opaque parse failure.
    """
    def __init__(self, code: str, server_message: str) -> None:
        super().__init__(f"[{code}] {server_message}")
        self.code = code
        self.server_message = server_message


def _booking_from_response(data: Dict[str, Any]) -> Booking:
    """Parse ``booking`` from a USP JSON response.

    Handles two non-spec response shapes:
    - Non-spec ``status`` values (for example ``"created"``), remapped via
      ``_NON_SPEC_STATUS_MAP`` with a WARNING.
    - HTTP 200 error bodies (no ``booking`` field, ``messages`` array with
      ``type="error"``) — raised as ``BookingServerError`` so callers can
      distinguish "booking not found" from a parse failure.
    """
    raw = data.get("booking")
    if raw is None:
        # Server returned HTTP 200 but no booking field.
        # Check for a USP error envelope: {"messages": [{"type": "error", ...}]}
        messages = data.get("messages") or []
        error_entries = [
            m for m in messages
            if isinstance(m, dict) and m.get("type") == "error"
        ]
        if error_entries:
            code = error_entries[0].get("code") or "unknown_error"
            content = error_entries[0].get("content") or ""
            logger.error(
                "USP server returned HTTP 200 with error body (no 'booking' field). "
                "Non-compliant: error responses MUST use 4xx/5xx HTTP status codes. "
                "Error code=%s message=%r. Full response keys: %s",
                code, content, list(data.keys()),
            )
            raise BookingServerError(code, content)
        # No error messages either — genuinely malformed response.
        logger.error(
            "USP response missing 'booking' field and no error messages. "
            "Response keys: %s. Body (truncated): %.500s",
            list(data.keys()), json.dumps(data),
        )
        raise ValueError(
            f"USP response missing 'booking' — got keys: {list(data.keys())}"
        )
    raw_status = raw.get("status")
    if raw_status in _NON_SPEC_STATUS_MAP:
        mapped = _NON_SPEC_STATUS_MAP[raw_status]
        logger.warning(
            "Non-spec booking status %r received from server — remapping to %r. "
            "The emitting business server is non-compliant with the USP spec "
            "(schemas/booking.json does not include %r).",
            raw_status, mapped, raw_status,
        )
        raw = {**raw, "status": mapped}
    messages = raw.get("messages")
    if isinstance(messages, list) and any(
        isinstance(message, dict) and message.get("severity") == ""
        for message in messages
    ):
        logger.warning(
            "USP booking response contained an empty optional message severity; "
            "treating it as absent."
        )
        raw = {
            **raw,
            "messages": [
                {key: value for key, value in message.items() if key != "severity"}
                if isinstance(message, dict) and message.get("severity") == ""
                else message
                for message in messages
            ],
        }
    return Booking.model_validate(raw)

class RevisionMismatch(Exception):
    """A conditional write was rejected because the resource moved on (HTTP 412).

    USP Section 5.6.  The write was **not** applied, so this is safe to recover from --
    but only by re-reading the resource and reconciling against the newer state.
    Blindly retrying without the new ``revision`` would either 412 again or, if
    the client dropped the precondition to make it succeed, clobber whatever
    change caused the mismatch, which is the exact outcome the precondition
    exists to prevent.

    Distinct from 409, which USP reserves for idempotency-key conflicts.
    """

    def __init__(self, message: str, *, sent_revision: Optional[str] = None):
        super().__init__(message)
        self.sent_revision = sent_revision


class PopAuthorizationError(Exception):
    """A proof-of-possession failure reported by the business (USP Section 10.1.1).

    All of these are 401 and all are terminal for the caller: there is no
    platform identity to retry as, so the client surfaces ``WWW-Authenticate``
    and hands off to ``booking_url`` rather than escalating.
    """

    def __init__(self, code: str, message: str, www_authenticate: Optional[str] = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.www_authenticate = www_authenticate


def _raise_for_pop_failure(response: httpx.Response) -> None:
    """Turn a 401 into a specific, actionable error where the business named one."""
    payload = usp_error_payload(response)
    code = payload.get("code")
    if code not in POP_ERROR_CODES:
        return
    message = payload.get("content") or payload.get("message") or ""
    hint = {
        ERR_POP_KEY_MISMATCH: (
            "the credential is bound to a different key than the one presented"
        ),
        ERR_POP_PROOF_MISSING: "the business required a proof and none was accepted",
        ERR_POP_PROOF_REPLAYED: "this proof's jti was already seen",
        ERR_POP_PROOF_INVALID: "the proof failed verification",
    }.get(code, "")
    raise PopAuthorizationError(
        code,
        f"{message} ({hint})" if hint else message,
        www_authenticate=response.headers.get("WWW-Authenticate"),
    )


_TOKEN_EXPIRY_MARGIN_SECONDS = 60
_OAUTH_CLIENT_KWARGS = {"timeout": 10.0, "follow_redirects": True}
_REST_CLIENT_KWARGS = {"timeout": 15.0, "follow_redirects": True}


class USPTokenStore:
    """Manages per-business OAuth credentials and bearer tokens.

    Credentials are persisted in a JSON file keyed by business REST endpoint::

        {
          "https://biz.example.com/usp/v1": {
            "client_id": "...",
            "client_secret": "...",
            "access_token": "...",
            "expires_at": 1740704400,
            "token_endpoint": "https://biz.example.com/oauth2/token",
            "auth_header": "Authorization"
          }
        }

    ``auth_header`` is optional and defaults to ``"Authorization"``.

    v2 stores these machine credentials at mode ``0600`` via ``write_private_text``.
    OS keychain encryption is deferred; ``0600`` is the accepted ceiling for merchant
    OAuth material on the agent host (distinct from consumer Link/Google identity).

    Resolution order for ``get_token(endpoint)``:
      1. Cached ``access_token`` that has not expired.
      2. ``client_id``/``client_secret`` present -> acquire a new token.
      3. When *allow_discovery* is true and OAuth is not marked unavailable for
         this endpoint -> discover metadata, register, then acquire.

    Callers without stored credentials should issue anonymous REST requests first
    and retry with ``get_token(..., force_discovery=True)`` only on HTTP 401.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        platform_name: Optional[str] = None,
        platform_contact: Optional[str] = None,
    ) -> None:
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._path: Optional[str] = None
        self._platform_name = platform_name
        self._platform_contact = platform_contact

        if not path:
            return
        self._path = os.path.expanduser(path)
        if not os.path.isfile(self._path):
            logger.warning("USP token store not found at %s", self._path)
            return
        with open(self._path) as f:
            self._entries = json.load(f)
        logger.info(
            "Loaded %d USP credential(s) from %s",
            len(self._entries), self._path,
        )

    # -- Public API ----------------------------------------------------------

    def get_auth_header_name(self, endpoint: Optional[str]) -> str:
        """Return the HTTP header name for authorization, defaulting to ``Authorization``."""
        if not endpoint:
            return "Authorization"
        return self._entries.get(endpoint, {}).get("auth_header", "Authorization")

    def has_stored_credentials(self, endpoint: Optional[str]) -> bool:
        """True when the token store can refresh a token without OAuth discovery."""
        if not endpoint:
            return False
        entry = self._entries.get(endpoint, {})
        if entry.get("client_id") and entry.get("client_secret"):
            return True
        token = entry.get("access_token")
        expires_at = entry.get("expires_at", 0)
        return bool(token and expires_at)

    def oauth_unavailable(self, endpoint: Optional[str]) -> bool:
        """True when OAuth metadata discovery previously failed for *endpoint*."""
        if not endpoint:
            return False
        return bool(self._entries.get(endpoint, {}).get("oauth_unavailable"))

    async def get_token(
        self,
        endpoint: Optional[str],
        *,
        allow_discovery: bool = True,
        force_discovery: bool = False,
    ) -> Optional[str]:
        """Return a valid bearer token for *endpoint*, acquiring one if needed."""
        if not endpoint:
            return None

        if force_discovery:
            self._clear_oauth_unavailable(endpoint)

        entry = self._entries.get(endpoint, {})

        token = entry.get("access_token")
        expires_at = entry.get("expires_at", 0)
        if token and time.time() < expires_at - _TOKEN_EXPIRY_MARGIN_SECONDS:
            return token

        client_id = entry.get("client_id")
        client_secret = entry.get("client_secret")
        token_endpoint = entry.get("token_endpoint")

        if not client_id or not client_secret:
            if not allow_discovery or self.oauth_unavailable(endpoint):
                return None
            registered = await self._discover_and_register(endpoint)
            if not registered:
                return None
            client_id, client_secret, token_endpoint = registered
            entry["client_id"] = client_id
            entry["client_secret"] = client_secret
            if token_endpoint:
                entry["token_endpoint"] = token_endpoint
            self._entries[endpoint] = entry

        if not token_endpoint:
            if not allow_discovery or self.oauth_unavailable(endpoint):
                return None
            metadata = await self._discover_oauth_metadata(endpoint)
            if not metadata:
                return None
            token_endpoint = metadata.get("token_endpoint")
            if not token_endpoint:
                logger.warning("OAuth metadata for %s has no token_endpoint", endpoint)
                return None
            entry["token_endpoint"] = token_endpoint
            self._entries[endpoint] = entry

        acquired = await self._acquire_token(
            token_endpoint, client_id, client_secret,
        )
        if not acquired:
            return None

        access_token, new_expires_at = acquired
        entry["access_token"] = access_token
        entry["expires_at"] = new_expires_at
        self._entries[endpoint] = entry
        self._save()
        return access_token

    # -- OAuth metadata discovery (RFC 8414) ---------------------------------

    def _record_metadata_failure(self, endpoint: str) -> None:
        entry = self._entries.setdefault(endpoint, {})
        entry["oauth_unavailable"] = True
        entry.pop("metadata_failed_at", None)
        self._entries[endpoint] = entry
        self._save()

    def _clear_oauth_unavailable(self, endpoint: str) -> None:
        entry = self._entries.get(endpoint)
        if not entry:
            return
        changed = False
        if entry.pop("oauth_unavailable", None):
            changed = True
        if entry.pop("metadata_failed_at", None):
            changed = True
        if changed:
            self._entries[endpoint] = entry
            self._save()

    async def _discover_oauth_metadata(
        self, endpoint: str,
    ) -> Optional[Dict[str, Any]]:
        if self.oauth_unavailable(endpoint):
            logger.debug(
                "Skipping OAuth metadata fetch for %s (oauth_unavailable)",
                endpoint,
            )
            return None

        base_url = self._base_url(endpoint)
        url = f"{base_url}/.well-known/oauth-authorization-server"
        try:
            async def _get() -> httpx.Response:
                async with traced_async_client(**_OAUTH_CLIENT_KWARGS) as client:
                    return await client.get(url)

            resp = await with_http_retry(_get)
            resp.raise_for_status()
            self._clear_oauth_unavailable(endpoint)
            return resp.json()
        except Exception:
            self._record_metadata_failure(endpoint)
            logger.warning("Failed to fetch OAuth metadata from %s", url, exc_info=True)
            return None

    # -- Dynamic Client Registration (RFC 7591) ------------------------------

    async def _discover_and_register(
        self, endpoint: str,
    ) -> Optional[Tuple[str, str, Optional[str]]]:
        """Discover OAuth metadata and register. Returns (client_id, client_secret, token_endpoint)."""
        metadata = await self._discover_oauth_metadata(endpoint)
        if not metadata:
            return None

        reg_endpoint = metadata.get("registration_endpoint")
        if not reg_endpoint:
            logger.warning(
                "OAuth metadata for %s has no registration_endpoint; "
                "provide client_id/client_secret manually in the token store",
                endpoint,
            )
            return None

        try:
            body: Dict[str, Any] = {
                "client_name": self._platform_name,
                "grant_types": ["client_credentials"],
                "token_endpoint_auth_method": "client_secret_basic",
            }
            if self._platform_contact:
                body["contacts"] = [self._platform_contact]

            async def _post() -> httpx.Response:
                async with traced_async_client(**_OAUTH_CLIENT_KWARGS) as client:
                    return await client.post(reg_endpoint, json=body)

            resp = await with_http_retry(_post)
            resp.raise_for_status()
            data = resp.json()

            client_id = data["client_id"]
            client_secret = data["client_secret"]
            token_endpoint = metadata.get("token_endpoint")

            logger.info(
                "Registered with %s as client_id=%s", endpoint, client_id,
            )
            self._save()
            return client_id, client_secret, token_endpoint

        except Exception:
            logger.warning(
                "Dynamic client registration failed for %s", endpoint, exc_info=True,
            )
            return None

    # -- Token acquisition (client_credentials grant) ------------------------

    async def _acquire_token(
        self,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
    ) -> Optional[Tuple[str, int]]:
        """Exchange client credentials for an access token. Returns (token, expires_at)."""
        try:
            async def _post() -> httpx.Response:
                async with traced_async_client(**_OAUTH_CLIENT_KWARGS) as client:
                    return await client.post(
                        token_endpoint,
                        data={"grant_type": "client_credentials"},
                        auth=(client_id, client_secret),
                    )

            resp = await with_http_retry(_post)
            resp.raise_for_status()
            data = resp.json()

            access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            expires_at = int(time.time()) + expires_in

            logger.info("Acquired USP token for %s (expires_in=%ds)", token_endpoint, expires_in)
            return access_token, expires_at

        except Exception:
            logger.warning(
                "Token acquisition failed at %s", token_endpoint, exc_info=True,
            )
            return None

    # -- Persistence ---------------------------------------------------------

    def _save(self) -> None:
        if not self._path:
            return
        try:
            write_private_text(Path(self._path), json.dumps(self._entries, indent=2))
        except Exception:
            logger.warning("Failed to persist token store to %s", self._path, exc_info=True)

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _base_url(endpoint: str) -> str:
        parsed = urlparse(endpoint)
        return f"{parsed.scheme}://{parsed.netloc}"


class BaseUSPRestClient:
    """Base async REST client for USP."""

    def __init__(
            self,
            bearer_token: Optional[str] = None,
            auth_header_name: str = "Authorization",
            token_store: Optional[USPTokenStore] = None,
            rest_endpoint: Optional[str] = None,
            pop_key: Optional[PopKey] = None,
            authenticate: bool = False,
            decision: Optional[AuthorizationDecision] = None,
    ):
        self.bearer_token = bearer_token
        self.auth_header_name = auth_header_name
        self._token_store = token_store
        self._rest_endpoint = rest_endpoint
        #: Ed25519 key for ``platform_key_pop``.  Present but unused when the
        #: negotiated posture does not authenticate.
        self._pop_key = pop_key
        #: True when the business advertised ``platform_key_pop`` and we selected it.
        self._authenticate = authenticate and pop_key is not None
        #: Carries a deferred refusal when the business cannot be transacted
        #: with under the current posture.  Enforced per request, on privileged
        #: operations only.
        self._decision = decision
        #: Latest non-empty ``DPoP-Nonce`` from any response (RFC 9449 rotation).
        #: In-memory only; a process restart recovers via a 401 challenge.
        self._dpop_nonce: Optional[str] = None

    def _store_dpop_nonce_from_response(self, response: httpx.Response) -> None:
        """Remember a non-empty ``DPoP-Nonce``; empty or missing does not clear."""
        nonce = dpop_nonce_from_response(response)
        if nonce is not None:
            self._dpop_nonce = nonce

    @property
    def pop_jkt(self) -> Optional[str]:
        return self._pop_key.jkt if self._pop_key else None

    def _headers(
        self,
        *,
        method: str,
        url: str,
        idempotency_key: Optional[str] = None,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
        nonce: Optional[str] = None,
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if if_match:
            headers["If-Match"] = if_match

        # A pre-provisioned bearer is an operator decision (the OAuth store or
        # BOOKING_AGENT_USP_MERCHANT_BEARER), so it travels on whatever the operator
        # configured it for, public reads included -- some deployments gate the
        # catalog.  This is the dormant path; see ``oauth_dormant`` and #199.
        if self.bearer_token:
            headers[self.auth_header_name] = f"Bearer {self.bearer_token}"

        if access == usp_methods.PUBLIC or not self._authenticate:
            # A *proof*, unlike a bearer, is never sent on a public operation.
            # Beyond being unnecessary, an unsolicited proof would establish a
            # trust-on-first-use binding of our key to a business we are only
            # browsing.
            return headers

        assert self._pop_key is not None  # guaranteed by ``_authenticate``
        # Under platform_key_pop the Authorization header is ours to define: at
        # issuance there must be none at all, and at presentation it carries the
        # credential under the DPoP scheme.  Drop any dormant bearer.
        headers.pop(self.auth_header_name, None)
        headers.pop("Authorization", None)
        ath: Optional[str] = None
        if credential is not None:
            # Presentation: the credential rides the DPoP *scheme* rather than
            # Bearer, so a business cannot silently accept a sender-constrained
            # credential as a plain bearer token.
            headers["Authorization"] = f"DPoP {credential.value}"
            ath = credential_ath(credential.value)
        # At issuance there is no credential, no ``ath``, and deliberately no
        # Authorization header at all -- the proof header *is* the credential.
        effective_nonce = nonce if nonce is not None else self._dpop_nonce
        headers["DPoP"] = build_rest_proof(
            self._pop_key, htm=method, htu=url, ath=ath, nonce=effective_nonce,
        )
        return headers

    async def _raw_request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
        nonce: Optional[str] = None,
    ) -> httpx.Response:
        # Headers are built here, against the *final* absolute URL, because the
        # proof binds ``htu`` to it.  Building the proof at a call site, before
        # the business-scoped path joining below, would produce an ``htu`` that
        # disagrees with the wire and fail every request as pop_proof_invalid.
        headers = self._headers(
            method=method,
            url=url,
            idempotency_key=idempotency_key,
            access=access,
            credential=credential,
            if_match=if_match,
            nonce=nonce,
        )
        async with traced_async_client(**_REST_CLIENT_KWARGS) as client:
            response = await client.request(
                method,
                url,
                json=json_body,
                headers=headers,
            )
        self._store_dpop_nonce_from_response(response)
        return response

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        idempotent: bool = False,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> dict:
        if access != usp_methods.PUBLIC and self._decision is not None:
            # Fail closed, here rather than at client construction: public
            # operations against this same business stay browsable.
            self._decision.raise_if_refused()

        logger.info("USP REST %s %s", method.upper(), url)

        # Generated once, deliberately outside ``_do``: a retry is another attempt
        # at the *same* logical call, so it must reuse the key or the business
        # cannot deduplicate it. Proof ``jti`` values, by contrast, are rebuilt per
        # attempt inside ``_headers``, which is what the replay cache requires.
        idempotency_key = str(uuid.uuid4()) if idempotent else None
        sent_nonce: Optional[str] = None

        async def _do() -> httpx.Response:
            nonlocal sent_nonce
            sent_nonce = self._dpop_nonce
            return await self._raw_request(
                method,
                url,
                json_body=json_body,
                idempotency_key=idempotency_key,
                access=access,
                credential=credential,
                if_match=if_match,
            )

        response = await with_http_retry(_do)

        if response.status_code == 401 and self._authenticate:
            challenge = nonce_challenge_from_401(response)
            if challenge is not None and challenge != sent_nonce:
                # A nonce challenge is a negotiation, not a failure: retry once
                # with a fresh proof carrying it.  Bounded at one attempt so a
                # business that always challenges cannot spin the client.
                self._dpop_nonce = challenge
                logger.info("Business challenged for a proof nonce; retrying once")
                response = await with_http_retry(_do)
            else:
                _raise_for_pop_failure(response)

        response = await self._maybe_retry_with_oauth(response, _do)

        if response.status_code == 412:
            # Surfaced as its own type rather than a bare HTTPStatusError: the
            # write did not happen and the caller must re-read and reconcile,
            # which is a different recovery from any other 4xx.
            raise RevisionMismatch(
                f"conditional write rejected: the resource changed since revision "
                f"{if_match!r} was read. Re-read it and reconcile before retrying.",
                sent_revision=if_match,
            )

        response.raise_for_status()
        return response.json() if response.content else {}

    async def _maybe_retry_with_oauth(self, response, _do):
        """Dormant OAuth 401 retry -- see ``oauth_dormant`` and spec issue #199.

        Never reached under ``platform_key_pop``: there is no platform identity
        to retry as, which is what makes the privilege-upgrade path that USP
        forbids structurally impossible rather than merely prohibited.
        """
        if (
            response.status_code == 401
            and not self._authenticate
            and self._token_store is not None
            and self._rest_endpoint
        ):
            token = await self._token_store.get_token(
                self._rest_endpoint,
                allow_discovery=True,
                force_discovery=True,
            )
            if token:
                self.bearer_token = token
                return await with_http_retry(_do)
        return response

    async def _post(
            self,
            url: str,
            body: Dict[str, Any],
            *,
            idempotent: bool = False,
            access: str = usp_methods.PUBLIC,
            credential: Optional[BookingScopedCredential] = None,
            if_match: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "POST", url, json_body=body, idempotent=idempotent,
            access=access, credential=credential, if_match=if_match,
        )

    async def _get(
        self,
        url: str,
        *,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
    ) -> dict:
        return await self._request("GET", url, access=access, credential=credential)

    async def _put(
        self,
        url: str,
        body: Dict[str, Any],
        *,
        idempotent: bool = False,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> dict:
        return await self._request(
            "PUT", url, json_body=body, idempotent=idempotent,
            access=access, credential=credential, if_match=if_match,
        )

    async def _delete(
        self,
        url: str,
        *,
        idempotent: bool = False,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
    ) -> dict:
        return await self._request(
            "DELETE", url, idempotent=idempotent, access=access, credential=credential,
        )


class USPBusinessRestClient(BaseUSPRestClient):
    """Async REST client for a single USP-compliant business."""

    def __init__(
        self,
        profile: USPBusinessProfile,
        agent_profile_url: Optional[str] = None,
        bearer_token: Optional[str] = None,
        auth_header_name: str = "Authorization",
        token_store: Optional[USPTokenStore] = None,
        rest_endpoint: Optional[str] = None,
        pop_key: Optional[PopKey] = None,
        authenticate: bool = False,
        ucp_native: bool = False,
        decision: Optional[AuthorizationDecision] = None,
    ):
        super().__init__(
            bearer_token,
            auth_header_name,
            token_store=token_store,
            rest_endpoint=rest_endpoint or profile.rest_endpoint,
            pop_key=pop_key,
            authenticate=authenticate,
            decision=decision,
        )
        self.endpoint = profile.rest_endpoint
        self.capabilities = profile.capabilities
        self.business_info = profile.business
        self.agent_profile_url = agent_profile_url
        #: UCP-Native deployments identify the platform with ``UCP-Agent``;
        #: ``USP-Agent`` is the Standalone header and is omitted there.
        self.ucp_native = ucp_native
        #: Set by the resource-creating methods; read by the caller that owns
        #: persistence.  Creation responses are the only place a credential
        #: appears -- it is never returned on a read.
        self.last_issued_credential: Optional[BookingScopedCredential] = None

        if not self.endpoint:
            raise ValueError(
                f"Business '{profile.business.name}' has no REST transport binding"
            )

    def _headers(
        self,
        *,
        method: str,
        url: str,
        idempotency_key: Optional[str] = None,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
        nonce: Optional[str] = None,
    ) -> Dict[str, str]:
        headers = super()._headers(
            method=method,
            url=url,
            idempotency_key=idempotency_key,
            access=access,
            credential=credential,
            if_match=if_match,
            nonce=nonce,
        )
        if self.agent_profile_url:
            # USP Section 10.1.6 requires the agent header on every privileged request.
            # Standalone uses USP-Agent; UCP-Native identifies the platform with
            # UCP-Agent instead, and omits USP-Agent.
            name = "UCP-Agent" if self.ucp_native else "USP-Agent"
            headers[name] = f'profile="{self.agent_profile_url}"'
        return headers

    async def _post(
        self,
        path: str,
        body: Dict[str, Any],
        *,
        idempotent: bool = False,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> dict:
        return await super()._post(url=f"{self.endpoint.rstrip('/')}{path}",
                                   body=body,
                                   idempotent=idempotent,
                                   access=access,
                                   credential=credential,
                                   if_match=if_match)

    async def _get(
        self,
        path: str,
        *,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
    ) -> dict:
        return await super()._get(url=f"{self.endpoint.rstrip('/')}{path}",
                                  access=access,
                                  credential=credential)

    async def _put(
        self,
        path: str,
        body: Dict[str, Any],
        *,
        idempotent: bool = False,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> dict:
        return await super()._put(url=f"{self.endpoint.rstrip('/')}{path}",
                                  body=body,
                                  idempotent=idempotent,
                                  access=access,
                                  credential=credential,
                                  if_match=if_match)

    async def _delete(
        self,
        path: str,
        *,
        idempotent: bool = False,
        access: str = usp_methods.PUBLIC,
        credential: Optional[BookingScopedCredential] = None,
    ) -> dict:
        return await super()._delete(url=f"{self.endpoint.rstrip('/')}{path}",
                                     idempotent=idempotent,
                                     access=access,
                                     credential=credential)

    def _guard_scope(
        self, credential: Optional[BookingScopedCredential], usp_method: str,
    ) -> Optional[BookingScopedCredential]:
        """Drop a credential that does not cover *usp_method* or has expired.

        Sending it anyway just earns a 401; the caller's expiry path (hand off to
        ``booking_url``) is the useful outcome and it triggers on absence.
        """
        if credential is None:
            return None
        if credential.is_expired():
            logger.info(
                "Booking-scoped credential expired at %s; omitting it from %s",
                credential.expires_at, usp_method,
            )
            return None
        if not credential.covers(usp_method):
            logger.info(
                "Booking-scoped credential scope %r does not cover %s; omitting it",
                credential.scope, usp_method,
            )
            return None
        return credential

    # ------------------------------------------------------------------
    # Catalog (Section 3.10)
    # ------------------------------------------------------------------

    async def list_services(
        self,
        filters: Optional[Dict[str, Any]] = None,
        pagination: Optional[Dict[str, Any]] = None,
    ) -> List[USPService]:
        body: Dict[str, Any] = {}
        if filters:
            body["filters"] = filters
        if pagination:
            body["pagination"] = pagination
        data = await self._post("/services/list", body)
        return [USPService(**s) for s in data.get("services", [])]

    async def get_service(self, service_id: str) -> USPService:
        data = await self._get(f"/services/{service_id}")
        return USPService.model_validate(data.get("service"))

    # ------------------------------------------------------------------
    # Availability (Section 4.3)
    # ------------------------------------------------------------------

    async def query_availability(
        self,
        service_id: str,
        start_date: str,
        end_date: str,
        *,
        timezone: Optional[str] = None,
        location_id: Optional[str] = None,
        party_size: Optional[int] = None,
        resource_id: Optional[str] = None,
    ) -> AvailabilityResult:
        body: Dict[str, Any] = {
            "service_id": service_id,
            "start_date": start_date,
            "end_date": end_date
        }
        if timezone:
            body["date_bounds_timezone"] = timezone
        if location_id:
            body["location_id"] = location_id
        if party_size:
            body["party_size"] = party_size
        if resource_id:
            body["resource_id"] = resource_id
        data = await self._post("/availability/query", body)
        return AvailabilityResult(
            [TimeSlot(**s) for s in data.get("slots", [])],
            business_timezone=data.get("business_timezone"),
            applied_date_bounds_timezone=data.get("applied_date_bounds_timezone"),
        )

    async def hold_slot(
        self,
        slot_id: str,
        service_id: str,
        spots: int = 1,
    ) -> Hold:
        body = {"slot_id": slot_id, "service_id": service_id, "spots": spots}
        data = await self._post(
            "/availability/holds", body, idempotent=True,
            access=usp_methods.PRIVILEGED_PLATFORM,
        )
        self.last_issued_credential = self._read_issued_credential(data)
        return Hold.model_validate(data.get("hold"))

    async def release_hold(
        self, hold_id: str, *, credential: Optional[BookingScopedCredential] = None,
    ) -> dict:
        # ``usp_availability_release`` is privileged_scoped, so it must go through
        # the shared request path: it previously called ``client.delete`` directly
        # and so carried no authorization and no 401 handling.
        return await self._delete(
            f"/availability/holds/{hold_id}", idempotent=True,
            access=usp_methods.PRIVILEGED_SCOPED,
            credential=self._guard_scope(credential, "usp_availability_release"),
        )

    # ------------------------------------------------------------------
    # Bookings (Section 5.3)
    # ------------------------------------------------------------------

    async def create_booking(
        self,
        service_id: str,
        slot_id: str,
        buyer: Dict[str, Any],
        *,
        hold_id: Optional[str] = None,
        party_size: int = 1,
        resource_id: Optional[str] = None,
        notes: Optional[str] = None,
        delivery_address: Optional[Dict[str, Any]] = None,
        recipient: Optional[Dict[str, Any]] = None,
        consent: Optional[Dict[str, Any]] = None,
        post_payment_return_request: Optional[PostPaymentReturnRequest] = None,
    ) -> Booking:
        body: Dict[str, Any] = {
            "service_id": service_id,
            "slot_id": slot_id,
            "buyer": buyer,
            "party_size": party_size,
        }
        if hold_id:
            body["hold_id"] = hold_id
        if resource_id:
            body["resource_id"] = resource_id
        if notes:
            body["notes"] = notes
        if delivery_address:
            body["delivery_address"] = delivery_address
        if recipient:
            body["recipient"] = recipient
        if consent:
            body["consent"] = consent
        if post_payment_return_request:
            body["post_payment_return_request"] = post_payment_return_request.model_dump(
                by_alias=True, exclude_none=True
            )
        data = await self._post(
            "/bookings", body, idempotent=True,
            access=usp_methods.PRIVILEGED_PLATFORM,
        )
        # Issuance: the credential arrives here and nowhere else.  Verified
        # against our own key before it is handed back to the caller.
        self.last_issued_credential = self._read_issued_credential(data)
        return _booking_from_response(data)

    async def get_booking(
        self, booking_id: str, *, credential: Optional[BookingScopedCredential] = None,
    ) -> Booking:
        data = await self._get(
            f"/bookings/{booking_id}",
            access=usp_methods.PRIVILEGED_SCOPED,
            credential=self._guard_scope(credential, "usp_bookings_get"),
        )
        return _booking_from_response(data)

    async def cancel_booking(
        self,
        booking_id: str,
        reason: Optional[str] = None,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        body: Dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        data = await self._post(
            f"/bookings/{booking_id}/cancel", body, idempotent=True,
            access=usp_methods.PRIVILEGED_SCOPED,
            credential=self._guard_scope(credential, "usp_bookings_cancel"),
            if_match=if_match,
        )
        return _booking_from_response(data)

    async def reschedule_booking(
        self,
        booking_id: str,
        new_slot_id: str,
        new_hold_id: Optional[str] = None,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        body: Dict[str, Any] = {"slot_id": new_slot_id}
        if new_hold_id:
            body["hold_id"] = new_hold_id
        data = await self._post(
            f"/bookings/{booking_id}/reschedule", body, idempotent=True,
            access=usp_methods.PRIVILEGED_SCOPED,
            credential=self._guard_scope(credential, "usp_bookings_reschedule"),
            if_match=if_match,
        )
        return _booking_from_response(data)

    async def confirm_payment(
        self,
        booking_id: str,
        payment_result: USPPaymentResult,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        data = await self._post(
            f"/bookings/{booking_id}/confirm-payment",
            {"payment_result": payment_result.model_dump(by_alias=True, exclude_none=True)},
            idempotent=True,
            access=usp_methods.PRIVILEGED_SCOPED,
            credential=self._guard_scope(credential, "usp_bookings_confirm_payment"),
            if_match=if_match,
        )
        return _booking_from_response(data)

    async def update_booking(
        self,
        booking_id: str,
        buyer: Optional[Dict[str, Any]] = None,
        recipient: Optional[Dict[str, Any]] = None,
        notes: Optional[str] = None,
        delivery_address: Optional[Dict[str, Any]] = None,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        body: Dict[str, Any] = {}
        if buyer:
            body["buyer"] = buyer
        if recipient:
            body["recipient"] = recipient
        if notes is not None:
            body["notes"] = notes
        if delivery_address is not None:
            body["delivery_address"] = delivery_address
        data = await self._put(
            f"/bookings/{booking_id}", body, idempotent=True,
            access=usp_methods.PRIVILEGED_SCOPED,
            credential=self._guard_scope(credential, "usp_bookings_update"),
            if_match=if_match,
        )
        return _booking_from_response(data)

    async def confirm_booking(
        self,
        booking_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        data = await self._post(
            f"/bookings/{booking_id}/confirm", {}, idempotent=True,
            access=usp_methods.PRIVILEGED_SCOPED,
            credential=self._guard_scope(credential, "usp_bookings_confirm"),
            if_match=if_match,
        )
        return _booking_from_response(data)

    def _read_issued_credential(
        self, data: Dict[str, Any],
    ) -> Optional[BookingScopedCredential]:
        """Extract and verify the credential from a creation response.

        Standalone returns it at the response-body root.  UCP-Native paid
        bookings return it inside the ``dev.usp-protocol.services.paid_bookings``
        extension, as a *sibling* of that extension's ``booking`` object -- USP
        must not invent members on the UCP checkout root, and a credential must
        never flow into ``BookingContext``, which platforms persist and
        re-display.
        """
        payload = issued_credential_payload(data)
        if payload is None:
            return None
        if self._pop_key is None:
            # Not authenticating; nothing to bind against.  Keep the value so the
            # caller can still use it as a plain bearer against a legacy business.
            return BookingScopedCredential.model_validate(payload)
        return parse_issued_credential(
            payload, own_jkt=self._pop_key.jkt, required=self._authenticate,
        )


REGISTRY_SEARCH_FILTER_KEYS = (
    "query",
    "location",
    "verticals",
    "categories",
    "price_range",
    "duration_range",
)


class RegistrySearchRejectedError(ValueError):
    """The registry refused a ``search_services`` request (USP §6.3, §9.4).

    Raised the same way whether the registry answered HTTP 200 with an error in
    ``messages[]``, a 400/422 Problem Details body, or the request was refused
    locally before sending (``http_status`` is ``None``).
    """

    def __init__(
        self,
        *,
        code: str,
        message: str,
        http_status: Optional[int],
        path: Optional[str] = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.path = path

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "http_status": self.http_status,
            "path": self.path,
        }


def _registry_rejection_in_body(
    data: Any, http_status: int
) -> Optional[RegistrySearchRejectedError]:
    if not isinstance(data, dict):
        return None
    for message in data.get("messages") or []:
        if isinstance(message, dict) and message.get("type") == "error":
            return RegistrySearchRejectedError(
                code=message.get("code") or "registry_error",
                message=message.get("content") or "The registry rejected the search.",
                http_status=http_status,
                path=message.get("path") or None,
            )
    usp = data.get("usp")
    if isinstance(usp, dict) and usp.get("status") == "error":
        return RegistrySearchRejectedError(
            code="registry_error",
            message="The registry reported an error without details.",
            http_status=http_status,
        )
    return None


def _registry_rejection_from_problem(
    response: httpx.Response,
) -> RegistrySearchRejectedError:
    status = response.status_code
    try:
        problem = response.json()
    except ValueError:
        problem = None
    if not isinstance(problem, dict):
        problem = {}
    problem_type = str(problem.get("type") or "")
    slug = problem_type.rstrip("/").rsplit("/", 1)[-1] if "/errors/" in problem_type else ""
    code = slug.replace("-", "_") if slug else (
        "invalid_request" if status == 400 else "validation_error"
    )
    message = (
        problem.get("detail")
        or problem.get("title")
        or (response.text or "").strip()[:500]
        or f"HTTP {status}"
    )
    return RegistrySearchRejectedError(
        code=code, message=str(message), http_status=status,
    )


def build_registry_search_body(
    query: str,
    *,
    location: Optional[ServiceSearchLocation] = None,
    verticals: Optional[List[str]] = None,
    categories: Optional[List[str]] = None,
    price_range: Optional[PriceRange] = None,
    duration_range: Optional[DurationRange] = None,
    desired_service_time_ranges: Optional[List[DesiredServiceTimePreference]] = None,
    prefer_sooner_availability_slots: Optional[bool] = None,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the JSON body for ``POST /registry/search_services``."""
    # Field names follow the spec's snake_case wire form (USP §6.3).
    body: Dict[str, Any] = {}
    query = (query or "").strip()
    if query:
        body["query"] = query
    if location:
        body["location"] = location.model_dump(mode="json", exclude_none=True)
    if verticals:
        body["verticals"] = verticals
    if categories:
        body["categories"] = categories
    if price_range:
        price = price_range.model_dump(mode="json", exclude_none=True)
        if price:
            body["price_range"] = price
    if duration_range:
        duration = duration_range.model_dump(mode="json", exclude_none=True)
        if duration:
            body["duration_range"] = duration
    has_filter = any(key in body for key in REGISTRY_SEARCH_FILTER_KEYS)
    if has_filter and desired_service_time_ranges:
        body["desired_service_time_ranges"] = [
            preference.model_dump(mode="json", exclude_none=True)
            for preference in desired_service_time_ranges
        ]
    if has_filter and prefer_sooner_availability_slots is False:
        body["prefer_sooner_availability_slots"] = False
    pagination_body: Dict[str, Any] = {}
    if limit is not None:
        pagination_body["limit"] = limit
    if cursor is not None:
        pagination_body["cursor"] = cursor
    if pagination_body:
        body["pagination"] = pagination_body
    return body


class USPRegistryRestClient(BaseUSPRestClient):
    """Async REST client for a USP registry."""

    def __init__(self, bearer_token: Optional[str] = None, auth_header_name: str = "Authorization"):
        super().__init__(bearer_token, auth_header_name)

    @staticmethod
    async def search_registry_for_services(
        registry_url: str,
        query: str,
        *,
        location: Optional[ServiceSearchLocation] = None,
        verticals: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        price_range: Optional[PriceRange] = None,
        duration_range: Optional[DurationRange] = None,
        desired_service_time_ranges: Optional[List[DesiredServiceTimePreference]] = None,
        prefer_sooner_availability_slots: Optional[bool] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Tuple[List[ServiceSearchResult], ServiceSearchPagination]:
        """Search the USP registry for services matching the given criteria.

        This is a registry-level operation independent of any per-business
        endpoint.  *registry_url* is the base URL of the USP registry API.

        Handles both camelCase and snake_case response field names.

        Deliberately builds its own client rather than going through
        ``_raw_request``: ``usp_registry_search_services`` is a *public* method
        (USP Section 9.2.2), so it carries no authorization and needs none.  This is not
        an oversight -- do not add a proof here.  Registry *writes*, which are
        privileged_platform, are not implemented in this client at all.
        """
        body = build_registry_search_body(
            query,
            location=location,
            verticals=verticals,
            categories=categories,
            price_range=price_range,
            duration_range=duration_range,
            desired_service_time_ranges=desired_service_time_ranges,
            prefer_sooner_availability_slots=prefer_sooner_availability_slots,
            limit=limit,
            cursor=cursor,
        )
        if not any(key in body for key in REGISTRY_SEARCH_FILTER_KEYS):
            raise RegistrySearchRejectedError(
                code="validation_error",
                message=(
                    "search_services needs at least one filter: query, location, "
                    "verticals, categories, price_range, or duration_range."
                ),
                http_status=None,
            )

        post_url = registry_search_post_url(registry_url)
        logger.info("USP Registry POST %s", post_url)
        body_json = body
        started = time.monotonic()

        async def _post() -> httpx.Response:
            async with traced_async_client(**_REST_CLIENT_KWARGS) as client:
                return await client.post(
                    post_url,
                    json=body_json,
                    headers={"Content-Type": "application/json"},
                )

        from booking_agent.observability import emit_event, registry_error_class

        try:
            response = await with_http_retry(_post)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            latency_ms = round((time.monotonic() - started) * 1000, 1)
            emit_event(
                "registry_search_error",
                error_class=registry_error_class(exc.response),
                latency_ms=latency_ms,
                status=exc.response.status_code,
            )
            if exc.response.status_code in (400, 422):
                raise _registry_rejection_from_problem(exc.response) from exc
            raise
        except Exception:
            latency_ms = round((time.monotonic() - started) * 1000, 1)
            emit_event(
                "registry_search_error",
                error_class="upstream_failure",
                latency_ms=latency_ms,
            )
            raise

        latency_ms = round((time.monotonic() - started) * 1000, 1)
        data = response.json()
        # Registries may report validation_error at HTTP 200 instead of the
        # 422 Problem Details the spec requires (USP §9.4.3).
        rejection = _registry_rejection_in_body(data, response.status_code)
        if rejection is not None:
            emit_event(
                "registry_search_error",
                error_class="malformed_request",
                latency_ms=latency_ms,
                status=response.status_code,
            )
            raise rejection
        emit_event("registry_search", ok=True, latency_ms=latency_ms)
        for message in data.get("messages") or []:
            if isinstance(message, dict):
                logger.info(
                    "USP Registry %s %s: %s",
                    message.get("type"), message.get("code"), message.get("content"),
                )

        if "results" in data:
            results_raw = data["results"]
        elif "services" in data:
            results_raw = data["services"]
        else:
            results_raw = []

        if "pagingMetadata" in data:
            paging_raw = data["pagingMetadata"]
        elif "paging_metadata" in data:
            paging_raw = data["paging_metadata"]
        elif "pagination" in data:
            paging_raw = data["pagination"]
        else:
            paging_raw = {}

        results = [ServiceSearchResult.model_validate(r) for r in results_raw]
        paging = ServiceSearchPagination.model_validate(paging_raw)
        return results, paging