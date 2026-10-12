"""UCP business profile document models (USP UCP-Native mode, GH-001).

Parses the JSON returned by ``GET {profile_url}`` where *profile_url* is the
full document URL from registry search (USP Section 6.1 / 7.2). Does not fetch
over the network; use :func:`fetch_ucp_profile_document` or httpx at call sites.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING, Union

import httpx

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .discovery import USPBusinessProfile

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .authorization import AuthorizationPolicy, parse_policy
from .namespace import CAP_CATALOG, CAP_PAID_BOOKINGS, SVC_SERVICES, check_origin_binding

CAP_CHECKOUT = "dev.ucp.shopping.checkout"


class UcpTransportBinding(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: str
    spec: Optional[str] = None
    transport: str = "rest"
    endpoint: str
    schema_url: Optional[str] = Field(None, alias="schema")


class UcpCapabilityEntry(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: str
    spec: Optional[str] = None
    schema_url: Optional[str] = Field(None, alias="schema")
    extends: Optional[Union[str, List[str]]] = None


class UcpPaymentHandler(BaseModel):
    """Subset of UCP ``payment_handlers`` entries needed for SPT (GH-004)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    id: str
    type: Optional[str] = None


class UcpBusinessBlock(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    name: Optional[str] = None
    timezone: Optional[str] = None
    currency: Optional[str] = None


class UcpNativeProfileDocument(BaseModel):
    """Typed view of the UCP profile JSON (``ucp`` + ``business`` keys).

    ``payment_handlers`` is modelled as ``dict[namespace_key, list[HandlerInstance]]``
    per the UCP spec (GH-060).  Each ``HandlerInstance.id`` (e.g. ``stripe_spt_h1``)
    is the value to use as ``handler_id`` in ``complete_checkout`` - never the outer
    namespace key such as ``com.stripe.agentic_commerce.shared_payment_token`` (GH-061).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    version: Optional[str] = None
    services: Dict[str, List[UcpTransportBinding]] = Field(default_factory=dict)
    capabilities: Dict[str, List[UcpCapabilityEntry]] = Field(default_factory=dict)
    payment_handlers: Dict[str, List[UcpPaymentHandler]] = Field(default_factory=dict)
    business: Optional[UcpBusinessBlock] = None

    def all_payment_handler_instances(self) -> List[UcpPaymentHandler]:
        """Return all handler instances across all namespace keys as a flat list."""
        out: List[UcpPaymentHandler] = []
        for instances in self.payment_handlers.values():
            out.extend(instances)
        return out


class UcpProfileValidationError(ValueError):
    """Raised when the profile does not satisfy UCP-Native paid-bookings preconditions (GH-022)."""


class UcpNativeContext(BaseModel):
    """Validated UCP-native context: endpoints + paid bookings via checkout flag."""

    model_config = ConfigDict()

    profile: UcpNativeProfileDocument
    raw_document: Dict[str, Any] = Field(default_factory=dict)
    paid_bookings_via_ucp_checkout: bool = True

    def ucp_rest_endpoint(self) -> str:
        """REST base for UCP shopping (checkout) calls."""
        return _first_rest_endpoint(self.profile.services, "dev.ucp.shopping")

    def usp_rest_endpoint(self) -> str:
        """REST base for USP catalog / availability / bookings."""
        return _first_rest_endpoint(self.profile.services, SVC_SERVICES)

    def requires_paid_bookings(self) -> bool:
        return CAP_PAID_BOOKINGS in self.profile.capabilities

    def usp_authorization(self) -> Optional[AuthorizationPolicy]:
        """The ``config.authorization`` policy on the ``dev.usp-protocol.services`` binding.

        This -- not a top-level ``authorization`` -- is where UCP-Native
        publishes its policy (USP Section 10.1.6).  Read from ``raw_document`` because
        ``UcpTransportBinding`` drops unmodelled members.

        Returns None when no policy is published *or* when a binding carries
        ``authorization`` as a direct member.  That second shape is malformed:
        ``schemas/usp.json`` declares it with a never-satisfiable schema so it is
        rejected rather than silently accepted under ``additionalProperties``,
        which would let a business believe it had published a policy that no
        conforming consumer reads.  We deliberately do **not** fall back to
        reading it -- treating it as "no policy published" routes the caller to
        the fail-closed path, which is the intended outcome.
        """
        policy, _ = self._usp_authorization_parts()
        return policy

    def has_misplaced_binding_authorization(self) -> bool:
        """True when a ``dev.usp-protocol.services`` binding declares a sibling ``authorization``."""
        _, misplaced = self._usp_authorization_parts()
        return misplaced

    def _usp_authorization_parts(self) -> tuple[Optional[AuthorizationPolicy], bool]:
        ucp = self.raw_document.get("ucp")
        services = ucp.get("services") if isinstance(ucp, dict) else None
        bindings = services.get(SVC_SERVICES) if isinstance(services, dict) else None
        if not isinstance(bindings, list):
            return None, False

        misplaced = False
        policy: Optional[AuthorizationPolicy] = None
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            if "authorization" in binding:
                logger.warning(
                    f"{SVC_SERVICES} binding declares a sibling 'authorization' member, "
                    "which USP prohibits; the policy MUST be published as "
                    "config.authorization. Treating this business as publishing no "
                    "policy."
                )
                misplaced = True
                continue
            config = binding.get("config")
            if isinstance(config, dict) and policy is None:
                policy = parse_policy(config.get("authorization"))
        if misplaced:
            return None, True
        return policy, False


def _first_rest_endpoint(services: Dict[str, List[UcpTransportBinding]], key: str) -> str:
    for binding in services.get(key, []):
        if binding.transport == "rest" and binding.endpoint:
            return binding.endpoint.rstrip("/")
    raise UcpProfileValidationError(f"No REST endpoint under ucp.services[{key!r}]")


def document_looks_like_ucp_native_profile(data: Dict[str, Any]) -> bool:
    """True when the document is a UCP profile (top-level ``ucp`` object)."""
    return isinstance(data.get("ucp"), dict)


def first_verified_ucp_native_service(
    services: List[Any],
    ucp_native_by_profile_url: Dict[str, Any],
) -> Optional[Any]:
    """Return the first service whose profile URL resolved as UCP-native USP."""
    for service in services:
        business = getattr(service, "business", None)
        profile_url = getattr(business, "profile_url", None) if business is not None else None
        service_id = getattr(service, "id", None)
        if profile_url and service_id and profile_url in ucp_native_by_profile_url:
            return service
    return None


def _ucp_document_is_usp_capable(ucp_block: Dict[str, Any]) -> bool:
    caps = ucp_block.get("capabilities")
    services = ucp_block.get("services")
    if not isinstance(caps, dict) or not isinstance(services, dict):
        return False
    if CAP_CATALOG not in caps:
        return False
    bindings = services.get(SVC_SERVICES)
    if not isinstance(bindings, list):
        return False
    return any(
        isinstance(binding, dict)
        and binding.get("transport") == "rest"
        and binding.get("endpoint")
        for binding in bindings
    )


def usp_business_profile_from_ucp_native(ctx: UcpNativeContext) -> "USPBusinessProfile":
    """Build a :class:`USPBusinessProfile` for catalog/availability REST (GH-003)."""
    from .discovery import (
        USPBusinessProfile,
        USPBusinessInfo,
        USPCapabilityEntry,
        USPTransportBinding,
    )

    p = ctx.profile
    usp_caps: Dict[str, List[USPCapabilityEntry]] = {}
    for key, entries in p.capabilities.items():
        if not key.startswith(f"{SVC_SERVICES}."):
            continue
        usp_caps[key] = [
            USPCapabilityEntry(version=e.version, spec=e.spec, schema_url=e.schema_url)
            for e in entries
        ]
    usp_bindings: List[USPTransportBinding] = []
    for b in p.services.get(SVC_SERVICES, []):
        usp_bindings.append(
            USPTransportBinding(
                version=b.version,
                spec=b.spec,
                transport=b.transport,
                endpoint=b.endpoint,
                schema_url=b.schema_url,
            )
        )
    name = (p.business.name if p.business and p.business.name else "Business")
    biz = USPBusinessInfo(
        name=name,
        timezone=p.business.timezone if p.business else None,
        currency=p.business.currency if p.business else None,
    )
    return USPBusinessProfile(
        version=str(p.version or "1.0"),
        services={SVC_SERVICES: usp_bindings},
        capabilities=usp_caps,
        checkout_systems=[],
        business=biz,
        # UCP-Native publishes the policy on the binding, not at the top level;
        # carry it across so the REST client sees one shape either way.
        authorization=ctx.usp_authorization(),
    )


def parse_ucp_native_context_from_document(
    data: Dict[str, Any],
    *,
    require_paid_bookings: bool = True,
) -> UcpNativeContext:
    """Parse and validate *data* as a UCP profile document (GH-001 + GH-022 gates).

    Expects top-level ``ucp`` (or UCP-shaped root) and optional ``business``.
    Paid-checkout callers keep ``require_paid_bookings=True``. General discovery
    passes ``False`` and accepts USP-capable profiles without checkout.
    """
    ucp_block = data.get("ucp", data)
    if not isinstance(ucp_block, dict):
        raise UcpProfileValidationError("Profile JSON must contain a 'ucp' object")

    caps_raw = ucp_block.get("capabilities") or {}
    if require_paid_bookings:
        if CAP_CHECKOUT not in caps_raw:
            raise UcpProfileValidationError(f"Missing capability {CAP_CHECKOUT!r}")
        if CAP_PAID_BOOKINGS not in caps_raw:
            raise UcpProfileValidationError(f"Missing capability {CAP_PAID_BOOKINGS!r}")

        paid_raw = caps_raw[CAP_PAID_BOOKINGS]
        if not isinstance(paid_raw, list) or not paid_raw:
            raise UcpProfileValidationError(f"{CAP_PAID_BOOKINGS} capability list is empty")
        paid_first = paid_raw[0]
        if not isinstance(paid_first, dict):
            raise UcpProfileValidationError(f"{CAP_PAID_BOOKINGS} entries must be objects")
        extends = paid_first.get("extends")
        if not extends:
            raise UcpProfileValidationError(
                f"{CAP_PAID_BOOKINGS} must declare extends pointing at checkout capability"
            )
        if extends != CAP_CHECKOUT:
            raise UcpProfileValidationError(
                f"{CAP_PAID_BOOKINGS}.extends must be {CAP_CHECKOUT!r}, got {extends!r}"
            )
    elif not _ucp_document_is_usp_capable(ucp_block):
        raise UcpProfileValidationError(
            "UCP profile has no USP service binding or catalog capability"
        )

    capabilities: Dict[str, List[UcpCapabilityEntry]] = {}
    for cap_id, raw_list in caps_raw.items():
        if not isinstance(raw_list, list):
            continue
        capabilities[cap_id] = [
            UcpCapabilityEntry(**e) for e in raw_list if isinstance(e, dict)
        ]
        for entry in capabilities[cap_id]:
            check_origin_binding(cap_id, spec=entry.spec, schema=entry.schema_url)

    services_raw = ucp_block.get("services") or {}
    services: Dict[str, List[UcpTransportBinding]] = {}
    for sk, entries in services_raw.items():
        if not isinstance(entries, list):
            continue
        services[sk] = [UcpTransportBinding(**e) for e in entries if isinstance(e, dict)]
        for binding in services[sk]:
            check_origin_binding(sk, spec=binding.spec, schema=binding.schema_url)

    ph_raw = ucp_block.get("payment_handlers") or {}
    payment_handlers: Dict[str, List[UcpPaymentHandler]] = {}
    if isinstance(ph_raw, list):
        # Flat list (legacy / shorthand) - preserve under a synthetic empty-string key
        # so the dict shape invariant holds and instance ids remain accessible.
        instances = [UcpPaymentHandler(**h) for h in ph_raw if isinstance(h, dict)]
        if instances:
            payment_handlers[""] = instances
    elif isinstance(ph_raw, dict):
        for ns_key, entries in ph_raw.items():
            if isinstance(entries, list):
                parsed = [UcpPaymentHandler(**h) for h in entries if isinstance(h, dict)]
                if parsed:
                    payment_handlers[ns_key] = parsed
            elif isinstance(entries, dict):
                payment_handlers[ns_key] = [UcpPaymentHandler(**entries)]

    business_raw = data.get("business") or ucp_block.get("business") or {}
    business: Optional[UcpBusinessBlock] = None
    if isinstance(business_raw, dict) and business_raw:
        business = UcpBusinessBlock(**business_raw)

    profile = UcpNativeProfileDocument(
        version=str(ucp_block.get("version") or "1.0"),
        services=services,
        capabilities=capabilities,
        payment_handlers=payment_handlers,
        business=business,
    )

    return UcpNativeContext(
        profile=profile,
        raw_document=dict(data),
        paid_bookings_via_ucp_checkout=(
            CAP_CHECKOUT in caps_raw and CAP_PAID_BOOKINGS in caps_raw
        ),
    )


async def fetch_and_resolve_business_profile(
    profile_url: str,
    *,
    use_cache: bool = True,
) -> Tuple["USPBusinessProfile", Optional[UcpNativeContext]]:
    """GET *profile_url* and return USP-shaped profile plus optional UCP-native context."""
    from booking_agent.http_retry import with_http_retry
    from booking_agent.http_trace import traced_async_client

    from .discovery import USPBusinessProfile, USPProfileResolver
    from .profile_cache import read_cached_profile, write_cached_profile

    if use_cache:
        cached = read_cached_profile(profile_url)
        if cached is not None:
            return cached

    headers = _profile_fetch_headers()

    async def _get() -> httpx.Response:
        async with traced_async_client(timeout=30.0, follow_redirects=True) as client:
            return await client.get(profile_url, headers=headers or None)

    response = await with_http_retry(_get)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Profile response must be a JSON object")
    if document_looks_like_ucp_native_profile(data):
        uctx = parse_ucp_native_context_from_document(
            data, require_paid_bookings=False
        )
        profile = usp_business_profile_from_ucp_native(uctx)
    else:
        profile = USPProfileResolver._parse_profile(data)
        uctx = None
    write_cached_profile(profile_url, profile, uctx)
    return profile, uctx


def _profile_fetch_headers() -> Dict[str, str]:
    """Optional ``Authorization`` for profile GET when the host requires it (GH-023)."""
    import os

    raw = os.environ.get("BOOKING_AGENT_PROFILE_FETCH_BEARER")
    if not raw or not str(raw).strip():
        return {}
    token = str(raw).strip()
    if token.lower().startswith("bearer "):
        return {"Authorization": token}
    return {"Authorization": f"Bearer {token}"}


async def fetch_ucp_profile_document(profile_url: str) -> Dict[str, Any]:
    """GET *profile_url* and return parsed JSON (no path mutation)."""
    from booking_agent.http_retry import with_http_retry
    from booking_agent.http_trace import traced_async_client

    async def _get() -> httpx.Response:
        async with traced_async_client(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(profile_url, headers=_profile_fetch_headers() or None)
        return resp

    response = await with_http_retry(_get)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise UcpProfileValidationError("Profile response must be a JSON object")
    return data
