"""An ``httpx.MockTransport`` USP business that enforces ``platform_key_pop``.

Proof code lives below ``_raw_request``, so it can only be exercised by a fake
that speaks HTTP.  ``MockUSPBackend`` sits at the backend-ABC level and never
reaches the REST client at all, and the older ``_FakeAsyncClient`` idiom patches
``httpx.AsyncClient`` with an object that bypasses the choke point -- neither
would test any of this.  Hence a transport.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import httpx

from booking_agent.discovery import (
    USPBusinessInfo,
    USPBusinessProfile,
    USPCapabilityEntry,
    USPTransportBinding,
)

from .pop_business import PopBusiness, ProofRejected

ENDPOINT = "https://mock-business.example/usp/v1"
PROFILE_URI = "https://mock-business.example/.well-known/usp"


def business_profile(
    *,
    endpoint: str = ENDPOINT,
    accepted_mechanisms: Optional[List[str]] = None,
    require_auth: bool = True,
    with_policy: bool = True,
) -> USPBusinessProfile:
    """A profile advertising bookings, and by default a platform_key_pop policy."""
    authorization = None
    if with_policy:
        authorization = {
            "privileged_operations_require_authentication": require_auth,
            "accepted_mechanisms": accepted_mechanisms
            or ["platform_key_pop", "http_message_signature"],
        }
    return USPBusinessProfile.model_validate({
        "version": "2026-08-14",
        "services": {
            "dev.usp-protocol.services": [
                USPTransportBinding(
                    version="2026-08-14", transport="rest", endpoint=endpoint,
                ).model_dump(by_alias=True)
            ]
        },
        "capabilities": {
            "dev.usp-protocol.services.bookings": [
                USPCapabilityEntry(version="2026-08-14").model_dump(by_alias=True)
            ]
        },
        "checkoutSystems": ["redirect"],
        "business": USPBusinessInfo(
            name="Mock Business", timezone="UTC", currency="USD",
        ).model_dump(by_alias=True),
        "authorization": authorization,
    })


class MockPopBusinessTransport:
    """Routes the booking subset of the USP REST API through ``PopBusiness``."""

    def __init__(
        self,
        business: Optional[PopBusiness] = None,
        *,
        endpoint: str = ENDPOINT,
        agent_profile_url: Optional[str] = None,
        agent_profile_doc: Optional[Dict[str, Any]] = None,
    ):
        self.business = business or PopBusiness(audience=PROFILE_URI)
        self.endpoint = endpoint.rstrip("/")
        self.agent_profile_url = agent_profile_url
        self.agent_profile_doc = agent_profile_doc
        self.fetched_agent_profiles: Dict[str, Dict[str, Any]] = {}
        self.requests: List[httpx.Request] = []
        self._bookings: Dict[str, Dict[str, Any]] = {}
        # slot_id -> {start, end, duration}. Reschedule copies these onto the booking.
        self.slots: Dict[str, Dict[str, Any]] = {}
        self._counter = 0

    # -- httpx plumbing ----------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def patch(self, monkeypatch) -> "MockPopBusinessTransport":
        transport = self.transport()

        def fake_traced(**kwargs):
            merged = dict(kwargs)
            merged.setdefault("follow_redirects", True)
            return httpx.AsyncClient(transport=transport, **merged)

        monkeypatch.setattr("booking_agent.rest_client.traced_async_client", fake_traced)
        return self

    # -- Routing -----------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (
            self.agent_profile_url
            and str(request.url) == self.agent_profile_url
            and request.method == "GET"
        ):
            return httpx.Response(200, json=self.agent_profile_doc)
        path = request.url.path[len(httpx.URL(self.endpoint).path):] or "/"
        try:
            return self._route(request, path)
        except ProofRejected as rejected:
            status, body = rejected.to_response()
            headers: Dict[str, str] = {"WWW-Authenticate": 'DPoP realm="usp"'}
            if rejected.nonce:
                headers["DPoP-Nonce"] = rejected.nonce
            return httpx.Response(
                status,
                json=body,
                headers=headers,
            )

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:
        method = request.method
        auth = request.headers.get("Authorization")
        dpop = request.headers.get("DPoP")
        agent = request.headers.get("USP-Agent")

        if path == "/bookings" and method == "POST":
            return self._create_booking(request, dpop, agent)

        if path.startswith("/bookings/"):
            rest = path[len("/bookings/"):]
            booking_id, _, action = rest.partition("/")
            self.business.verify(
                method=method,
                url=str(request.url),
                authorization=auth,
                dpop=dpop,
                agent_profile=_profile_uri(agent),
                resource_id=booking_id,
            )
            booking = self._bookings.get(booking_id)
            if booking is None:
                return httpx.Response(404, json={"error": {"code": "not_found"}})

            # Conditional write (USP Section 5.6): a stale precondition is rejected
            # with 412 and the write is NOT applied, which is what stops two
            # agents holding the same credential from clobbering each other.
            if_match = request.headers.get("If-Match")
            if action and if_match and if_match != booking.get("revision"):
                return httpx.Response(
                    412, json={"error": {"code": "revision_mismatch"}},
                )

            if action == "cancel":
                booking = {**booking, "status": "canceled", "revision": _next_revision(booking)}
                self._bookings[booking_id] = booking
                if auth:
                    self.business.invalidate(auth.partition(" ")[2].strip())
                return httpx.Response(
                    200, json={"booking": booking, "cancellation_fee": {"amount": 0, "currency": "USD"}},
                )
            if action == "reschedule":
                body_in = json.loads(request.content or b"{}")
                slot_id = body_in.get("slot_id")
                if not slot_id:
                    return httpx.Response(400, json={"error": {"code": "invalid_request"}})
                slot = {**booking["slot"], "id": slot_id}
                known = self.slots.get(slot_id) or {}
                for key in ("start", "end", "duration"):
                    if known.get(key):
                        slot[key] = known[key]
                booking = {
                    **booking,
                    "slot": slot,
                    "revision": _next_revision(booking),
                }
                self._bookings[booking_id] = booking
                return httpx.Response(200, json={"booking": booking})
            return httpx.Response(200, json={"booking": booking})

        # Public tier: no proof expected, and the transport asserts none arrives.
        if path in ("/services/list", "/availability/query"):
            assert "DPoP" not in request.headers, (
                "a proof must never be sent on a public operation"
            )
            return httpx.Response(200, json={"services": [], "slots": []})

        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def _create_booking(
        self, request: httpx.Request, dpop: Optional[str], agent: Optional[str],
    ) -> httpx.Response:
        # Issuance: a proof and no credential.  An Authorization header here
        # would mean the client failed to drop its dormant bearer.
        assert "Authorization" not in request.headers, (
            "the creation call must carry no Authorization header at all"
        )
        agent_profile = _profile_uri(agent)
        self._fetch_agent_profile(agent_profile)
        self.business.verify(
            method="POST",
            url=str(request.url),
            authorization=None,
            dpop=dpop,
            agent_profile=agent_profile,
        )
        self._counter += 1
        booking_id = f"bk_{self._counter}"
        body_in = json.loads(request.content or b"{}")
        booking = {
            "id": booking_id,
            "service_id": body_in.get("service_id", "svc_1"),
            "service_name": "Mock Service",
            "status": "confirmed",
            "slot": {
                "id": body_in.get("slot_id", "slot_1"),
                "service_id": body_in.get("service_id", "svc_1"),
                "start": "2026-09-01T10:00:00Z",
                "end": "2026-09-01T11:00:00Z",
                "duration": "PT1H",
                "state": "available",
            },
            "buyer": {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
            "party_size": body_in.get("party_size", 1),
            "confirmation_mode": "auto",
            "created_at": "2026-08-15T10:00:00Z",
            "updated_at": "2026-08-15T10:00:00Z",
            # Presence of ``revision`` IS the advertisement that this business
            # supports conditional writes.
            "revision": "rev-1",
        }
        self._bookings[booking_id] = booking
        body: Dict[str, Any] = {
            "booking": booking,
            "booking_url": f"https://mock-business.example/bookings/{booking_id}",
        }
        if dpop:
            body["booking_scoped_credential"] = self.business.issue(booking_id, dpop=dpop)
        return httpx.Response(201, json=body)

    def _fetch_agent_profile(self, url: Optional[str]) -> None:
        if self.agent_profile_url is None or not url or url in self.fetched_agent_profiles:
            return
        with httpx.Client(transport=self.transport()) as client:
            response = client.get(url)
            response.raise_for_status()
            document = response.json()
        if not isinstance(document, dict):
            raise ProofRejected("pop_proof_invalid", "agent profile is not a JSON object")
        self.fetched_agent_profiles[url] = document


def _next_revision(booking: Dict[str, Any]) -> str:
    """Bump the concurrency token, as any mutation must."""
    current = booking.get("revision") or "rev-0"
    return f"rev-{int(current.rsplit('-', 1)[1]) + 1}"


def _bare_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(str(url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _profile_uri(agent_header: Optional[str]) -> Optional[str]:
    if not agent_header:
        return None
    prefix = 'profile="'
    if agent_header.startswith(prefix) and agent_header.endswith('"'):
        return agent_header[len(prefix):-1]
    return agent_header
