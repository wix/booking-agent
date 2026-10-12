"""USP Business Profile Discovery.

Implements the USP specification's /.well-known/usp profile discovery (Section 7.2).
Live HTTP discovery only.
"""
import logging
from typing import Any, Optional, Dict, List
from urllib.parse import urlparse

import httpx

from booking_agent.authorization import AuthorizationPolicy, parse_policy
from booking_agent.http_retry import with_http_retry
from booking_agent.http_trace import traced_async_client
from booking_agent.namespace import (
    CAP_AVAILABILITY,
    CAP_BOOKINGS,
    CAP_CATALOG,
    SVC_SERVICES,
    check_origin_binding,
)
from pydantic import BaseModel, Field, ConfigDict
from pydantic.alias_generators import to_camel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile models
# ---------------------------------------------------------------------------

class USPTransportBinding(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: str
    spec: Optional[str] = None
    transport: str
    endpoint: str
    schema_url: Optional[str] = Field(None, alias="schema")


class USPCapabilityEntry(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: str
    spec: Optional[str] = None
    schema_url: Optional[str] = Field(None, alias="schema")


class USPBusinessInfo(BaseModel):
    """schemas/usp.json#/$defs/BusinessInfo.

    Spec REQUIRES (name, timezone, currency). The client accepts documents
    without ``timezone`` or ``currency``, a deliberate permissive divergence.
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    timezone: Optional[str] = None
    currency: Optional[str] = None
    #: Business locations, referenced by ``Service.locations[]`` via their ``id``.
    #: Kept loosely typed: this is display and map data the client passes through
    #: rather than reasons about, and the spec's entry shape is inlined rather
    #: than named as a ``$def``.
    locations: Optional[List[Dict[str, Any]]] = None


class USPBusinessProfile(BaseModel):
    """Parsed /.well-known/usp profile."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    version: str
    services: Dict[str, List[USPTransportBinding]] = {}
    capabilities: Dict[str, List[USPCapabilityEntry]] = {}
    checkout_systems: List[str] = []
    business: USPBusinessInfo
    #: Standalone Mode: the top-level ``authorization`` policy (USP Section 10.1.6).
    #: In UCP-Native Mode the canonical policy is ``config.authorization`` on the
    #: ``dev.usp-protocol.services`` binding instead -- see ``ucp_profile``.
    authorization: Optional[AuthorizationPolicy] = None

    @property
    def exposes_privileged_capabilities(self) -> bool:
        """True when the business advertises anything beyond browse-only.

        Bookings imply create/cancel; a catalog alone does not.
        """
        return self.has_bookings

    @property
    def rest_endpoint(self) -> Optional[str]:
        for binding in self.services.get(SVC_SERVICES, []):
            if binding.transport == "rest":
                return binding.endpoint
        return None

    @property
    def has_catalog(self) -> bool:
        return CAP_CATALOG in self.capabilities

    @property
    def has_availability(self) -> bool:
        return CAP_AVAILABILITY in self.capabilities

    @property
    def has_bookings(self) -> bool:
        return CAP_BOOKINGS in self.capabilities


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class USPProfileResolver:
    """Resolves a business's USP profile via live HTTP from /.well-known/usp."""

    async def resolve(self, business_base_url: str) -> USPBusinessProfile:
        """Fetch GET {business_base_url}/.well-known/usp and parse the profile."""
        url = business_base_url.rstrip("/") + "/.well-known/usp"
        logger.info("Fetching USP profile from %s", url)

        async def _get() -> httpx.Response:
            async with traced_async_client(timeout=10.0, follow_redirects=True) as client:
                return await client.get(url)

        response = await with_http_retry(_get)
        response.raise_for_status()
        return self._parse_profile(response.json())

    async def resolve_from_url(self, usp_profile_url: str) -> USPBusinessProfile:
        """Resolve a business's USP profile from a direct profile URL.

        Used when the profile URL is already known (e.g. from a registry
        search result's ``business.usp_profile_url``).
        """
        logger.info("Fetching USP profile from %s", usp_profile_url)
        async def _get() -> httpx.Response:
            async with traced_async_client(timeout=10.0, follow_redirects=True) as client:
                return await client.get(usp_profile_url)

        response = await with_http_retry(_get)
        response.raise_for_status()
        return self._parse_profile(response.json())

    @staticmethod
    def _parse_profile(data: dict) -> USPBusinessProfile:
        usp_block = data.get("usp", data)
        return USPBusinessProfile(
            version=usp_block["version"],
            services=_parse_transport_bindings(usp_block.get("services", {})),
            capabilities=_parse_capabilities(usp_block.get("capabilities", {})),
            checkout_systems=usp_block.get("checkout_systems", []),
            business=USPBusinessInfo(**usp_block["business"]),
            authorization=parse_policy(usp_block.get("authorization")),
        )

    @staticmethod
    def extract_base_url_from_agent_card_url(agent_card_url: str) -> str:
        """Derive business base URL from a merchant A2A agent card URL.

        Strips ``/.well-known/agent.json`` or similar suffixes.
        """
        parsed = urlparse(agent_card_url)
        return f"{parsed.scheme}://{parsed.netloc}"


def _parse_transport_bindings(
    raw: Dict[str, list],
) -> Dict[str, List[USPTransportBinding]]:
    result: Dict[str, List[USPTransportBinding]] = {}
    for key, entries in raw.items():
        bindings = [USPTransportBinding(**e) for e in entries]
        for binding in bindings:
            check_origin_binding(key, spec=binding.spec, schema=binding.schema_url)
        result[key] = bindings
    return result


def _parse_capabilities(
    raw: Dict[str, list],
) -> Dict[str, List[USPCapabilityEntry]]:
    result: Dict[str, List[USPCapabilityEntry]] = {}
    for key, entries in raw.items():
        caps = [USPCapabilityEntry(**e) for e in entries]
        for cap in caps:
            check_origin_binding(key, spec=cap.spec, schema=cap.schema_url)
        result[key] = caps
    return result
