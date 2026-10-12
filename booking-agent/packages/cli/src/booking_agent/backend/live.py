"""Live USP backend for production paths.

Delegates registry search to a real USP registry, profile resolution to
USPProfileResolver (live HTTP only), and all business-level operations to
USPBusinessRestClient. No local booking cache.
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import load_config
from ..discovery import USPBusinessProfile, USPProfileResolver
from ..http_retry import is_rate_limited
from ..merchant_pacing import (
    discovery_concurrency,
    merchant_host_from_url,
    paced_merchant_host_request,
)
from ..models.platform import ServiceSearchIntent, ServiceSearchResult
from ..models.wire import (
    AvailabilityResult,
    Booking,
    Hold,
    TimeSlot,
    USPPaymentResult,
    USPService,
)
from ..rest_client import (
    USPBusinessRestClient,
    USPRegistryRestClient,
    USPTokenStore,
)
from ..headless import attach_registry_hit, mark_live_catalog_service, service_from_registry_hit
from ..authorization import BookingScopedCredential, evaluate, resolve_posture
from ..pop import PopKey, PopKeyStore
from ..ucp_profile import UcpNativeContext, fetch_and_resolve_business_profile
from . import USPBackend

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_SEARCH_LIMIT = 5


class LiveUSPBackend(USPBackend):
    """Production backend that talks to real USP endpoints."""

    def __init__(self) -> None:
        cfg = load_config()
        self._cfg = cfg
        self._resolver = USPProfileResolver()
        self._agent_profile_url = cfg.usp_agent_profile_url
        self._token_store = USPTokenStore(cfg.usp_platform_name, cfg.usp_platform_contact)
        self._posture = resolve_posture(cfg.usp_auth_scoped_credential)
        self._pop_keys = PopKeyStore()
        self.profile_cache: Dict[str, USPBusinessProfile] = {}
        self.ucp_native_by_business_id: Dict[str, UcpNativeContext] = {}
        self.ucp_native_by_profile_url: Dict[str, UcpNativeContext] = {}

    async def _fetch_and_resolve_profile(
        self, profile_url: str,
    ) -> Tuple[USPBusinessProfile, Optional[UcpNativeContext]]:
        return await fetch_and_resolve_business_profile(profile_url)

    async def _client(
        self,
        profile: USPBusinessProfile,
        *,
        ucp_native: bool = False,
        pop_key_endpoint: Optional[str] = None,
    ) -> USPBusinessRestClient:
        endpoint = profile.rest_endpoint
        auth_header = self._token_store.get_auth_header_name(endpoint)
        bearer: Optional[str] = None
        if self._token_store.has_stored_credentials(endpoint):
            bearer = await self._token_store.get_token(endpoint, allow_discovery=True)

        # Negotiate the mechanism from the business's published policy.
        #
        # ``defer=True`` because this one client serves public operations too.
        # A business that publishes no usable policy must still be *browsable*
        # -- catalog and availability MAY remain unauthenticated -- so the
        # refusal is carried on the decision and enforced when a privileged
        # request is actually attempted, not here.
        decision = evaluate(
            profile.authorization,
            posture=self._posture,
            exposes_privileged_capabilities=profile.exposes_privileged_capabilities,
            business_label=profile.business.name,
            defer=True,
        )
        pop_key: Optional[PopKey] = None
        if decision.uses_pop and endpoint:
            if pop_key_endpoint:
                # A credential may be issued on a related UCP binding and then
                # presented to the USP booking resource. Reuse that issuer key;
                # generating a key for the request origin would not match cnf.jkt.
                pop_key = self._pop_keys.peek(pop_key_endpoint)
            else:
                # First contact generates the key locally. There is no registration
                # round trip: this is the whole of platform onboarding.
                pop_key = self._pop_keys.get_or_create(endpoint)

        return USPBusinessRestClient(
            profile,
            agent_profile_url=self._agent_profile_url,
            bearer_token=bearer,
            auth_header_name=auth_header,
            token_store=self._token_store,
            rest_endpoint=endpoint,
            pop_key=pop_key,
            authenticate=decision.authenticate,
            ucp_native=ucp_native,
            decision=decision,
        )

    @staticmethod
    def _log_profile_failure(profile_url: str, exc: BaseException) -> None:
        if is_rate_limited(exc):
            logger.warning(
                "Profile resolution rate limited (HTTP 429) for %s",
                profile_url,
            )
            return
        if isinstance(exc, httpx.HTTPStatusError):
            logger.warning(
                "Profile resolution HTTP %s for %s",
                exc.response.status_code,
                profile_url,
            )
            return
        logger.warning("Profile resolution failed for %s", profile_url)

    @staticmethod
    def _log_service_failure(service_id: str, exc: BaseException) -> None:
        if is_rate_limited(exc):
            logger.warning(
                "Service catalog fetch rate limited (HTTP 429) for %s",
                service_id,
            )
            return
        if isinstance(exc, httpx.HTTPStatusError):
            logger.warning(
                "Service catalog fetch HTTP %s for %s",
                exc.response.status_code,
                service_id,
            )
            return
        logger.warning("Service fetch failed: %s", service_id)

    # -- Registry-level ------------------------------------------------------

    async def _resolve_profile_for_discovery(
        self,
        biz_id: str,
        profile_url: str,
        profile_unavailable: set[str],
    ) -> None:
        from booking_agent.profile_cache import read_cached_profile

        if biz_id in self.profile_cache or biz_id in profile_unavailable:
            return

        cached = read_cached_profile(profile_url)
        if cached is not None:
            prof, uctx = cached
            self.profile_cache[biz_id] = prof
            if uctx is not None:
                self.ucp_native_by_business_id[biz_id] = uctx
                self.ucp_native_by_profile_url[profile_url] = uctx
            return

        host = merchant_host_from_url(profile_url)
        try:
            async with paced_merchant_host_request(host):
                prof, uctx = await self._fetch_and_resolve_profile(profile_url)
            self.profile_cache[biz_id] = prof
            if uctx is not None:
                self.ucp_native_by_business_id[biz_id] = uctx
                self.ucp_native_by_profile_url[profile_url] = uctx
        except Exception as exc:
            profile_unavailable.add(biz_id)
            self._log_profile_failure(profile_url, exc)
            if is_rate_limited(exc):
                raise

    async def search_registry(self, intent: ServiceSearchIntent) -> List[USPService]:
        if not self._cfg.usp_registry_url:
            raise RuntimeError(
                "USP_REGISTRY_URL is not configured; cannot search the live registry"
            )
        registry_client = USPRegistryRestClient()
        from booking_agent.step_timing import timed_step

        async with timed_step("discover"):
            results, _paging = await registry_client.search_registry_for_services(
                registry_url=self._cfg.usp_registry_url,
                query=intent.query,
                location=intent.location,
                verticals=intent.verticals or None,
                categories=intent.categories or None,
                price_range=intent.price_range,
                duration_range=intent.duration_range,
                desired_service_time_ranges=intent.desired_service_time_ranges,
                prefer_sooner_availability_slots=intent.prefer_sooner_availability_slots,
                limit=DEFAULT_REGISTRY_SEARCH_LIMIT,
            )
        if len(results) > DEFAULT_REGISTRY_SEARCH_LIMIT:
            logger.info(
                "Registry returned %d hits; truncating to client limit %d",
                len(results),
                DEFAULT_REGISTRY_SEARCH_LIMIT,
            )
            results = results[:DEFAULT_REGISTRY_SEARCH_LIMIT]

        profile_unavailable: set[str] = set()
        profile_tasks: Dict[str, asyncio.Task[None]] = {}
        profile_tasks_lock = asyncio.Lock()
        sem = asyncio.Semaphore(discovery_concurrency())
        rate_limited_exc: Optional[BaseException] = None

        async def ensure_profile(biz_id: str, profile_url: str) -> Optional[USPBusinessProfile]:
            if biz_id in profile_unavailable:
                return None
            if biz_id in self.profile_cache:
                return self.profile_cache[biz_id]

            async with profile_tasks_lock:
                if biz_id in profile_unavailable:
                    return None
                if biz_id in self.profile_cache:
                    return self.profile_cache[biz_id]
                if biz_id not in profile_tasks:
                    profile_tasks[biz_id] = asyncio.create_task(
                        self._resolve_profile_for_discovery(
                            biz_id, profile_url, profile_unavailable,
                        ),
                    )
                task = profile_tasks[biz_id]

            await task
            if biz_id in profile_unavailable:
                return None
            return self.profile_cache.get(biz_id)

        async def enrich_hit(rank: int, r: ServiceSearchResult) -> Optional[USPService]:
            nonlocal rate_limited_exc
            if rate_limited_exc is not None:
                return None
            if not r.service_id or not r.business or not r.business.id:
                logger.warning(
                    "Skipping incomplete search result: %s",
                    r.model_dump(exclude_none=True),
                )
                return None

            async with sem:
                if rate_limited_exc is not None:
                    return None
                biz_id = r.business.id
                profile_url = r.business.usp_profile_url
                try:
                    prof = await ensure_profile(biz_id, profile_url)
                except Exception as exc:
                    if is_rate_limited(exc):
                        rate_limited_exc = exc
                    raise

                if prof is None:
                    return attach_registry_hit(
                        service_from_registry_hit(r), r, rank=rank,
                    )

                catalog_host = (
                    merchant_host_from_url(prof.rest_endpoint)
                    if prof.rest_endpoint
                    else merchant_host_from_url(profile_url)
                )
                try:
                    async with paced_merchant_host_request(catalog_host):
                        svc = await self.get_service(prof, r.service_id)
                    svc.business = r.business
                    return attach_registry_hit(
                        mark_live_catalog_service(svc), r, rank=rank,
                    )
                except Exception as exc:
                    self._log_service_failure(r.service_id, exc)
                    if is_rate_limited(exc):
                        rate_limited_exc = exc
                        raise
                    return attach_registry_hit(
                        service_from_registry_hit(r), r, rank=rank,
                    )

        services: List[USPService] = []
        async with timed_step("profile_resolve"):
            enrich_tasks = [
                asyncio.create_task(enrich_hit(rank, r))
                for rank, r in enumerate(results)
            ]
            try:
                outcomes = await asyncio.gather(*enrich_tasks, return_exceptions=True)
            except Exception:
                for task in enrich_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*enrich_tasks, return_exceptions=True)
                raise

            if rate_limited_exc is not None:
                for task in enrich_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*enrich_tasks, return_exceptions=True)
                raise rate_limited_exc

            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    if is_rate_limited(outcome):
                        for task in enrich_tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*enrich_tasks, return_exceptions=True)
                        raise outcome
                    logger.warning("Discovery enrich task failed: %s", outcome)
                    continue
                if outcome is not None:
                    services.append(outcome)

        services.sort(
            key=lambda svc: (svc.metadata or {}).get("search_rank", 10**9),
        )
        return services

    # -- Discovery -----------------------------------------------------------

    async def resolve_profile(self, usp_profile_url: str) -> USPBusinessProfile:
        prof, uctx = await self._fetch_and_resolve_profile(usp_profile_url)
        if uctx is not None:
            self.ucp_native_by_profile_url[usp_profile_url] = uctx
        return prof

    async def resolve_profile_from_agent_card(self, agent_card_url: str) -> USPBusinessProfile:
        base_url = USPProfileResolver.extract_base_url_from_agent_card_url(agent_card_url)
        return await self._resolver.resolve(base_url)

    # -- Catalog -------------------------------------------------------------

    async def list_services(
        self,
        profile: USPBusinessProfile,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[USPService]:
        client = await self._client(profile)
        return await client.list_services(filters=filters)

    async def get_service(self, profile: USPBusinessProfile, service_id: str) -> USPService:
        client = await self._client(profile)
        return await client.get_service(service_id)

    # -- Availability --------------------------------------------------------

    async def query_availability(
        self,
        profile: USPBusinessProfile,
        service_id: str,
        start_date: str,
        end_date: str,
        timezone: Optional[str] = None,
    ) -> AvailabilityResult:
        client = await self._client(profile)
        return await client.query_availability(
            service_id=service_id,
            start_date=start_date,
            end_date=end_date,
            timezone=timezone,
        )

    async def hold_slot(
        self,
        profile: USPBusinessProfile,
        slot_id: str,
        service_id: str,
        spots: int = 1,
    ) -> Hold:
        client = await self._client(profile)
        hold = await client.hold_slot(slot_id, service_id, spots)
        self.last_issued_credential = client.last_issued_credential
        return hold

    async def release_hold(
        self,
        profile: USPBusinessProfile,
        hold_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
    ) -> dict:
        client = await self._client(profile)
        return await client.release_hold(hold_id, credential=credential)

    # -- Booking -------------------------------------------------------------

    async def create_booking(
        self,
        profile: USPBusinessProfile,
        service_id: str,
        slot_id: str,
        buyer: Dict[str, Any],
        party_size: int = 1,
        resource_id: Optional[str] = None,
        notes: Optional[str] = None,
        delivery_address: Optional[Dict[str, Any]] = None,
    ) -> Booking:
        client = await self._client(profile)
        booking = await client.create_booking(
            service_id=service_id,
            slot_id=slot_id,
            buyer=buyer,
            party_size=party_size,
            resource_id=resource_id,
            notes=notes,
            delivery_address=delivery_address,
        )
        # The only point at which a credential is issued.  The caller that owns
        # session state persists it against (endpoint, booking id).
        self.last_issued_credential = client.last_issued_credential
        return booking

    async def get_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
    ) -> Booking:
        client = await self._client(
            profile,
            pop_key_endpoint=credential_endpoint if credential is not None else None,
        )
        return await client.get_booking(booking_id, credential=credential)

    async def cancel_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        reason: Optional[str] = None,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        client = await self._client(
            profile,
            pop_key_endpoint=credential_endpoint if credential is not None else None,
        )
        return await client.cancel_booking(
            booking_id, reason=reason, credential=credential, if_match=if_match,
        )

    async def reschedule_booking(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        new_slot_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
        credential_endpoint: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        client = await self._client(
            profile,
            pop_key_endpoint=credential_endpoint if credential is not None else None,
        )
        return await client.reschedule_booking(
            booking_id, new_slot_id, credential=credential, if_match=if_match,
        )

    async def confirm_payment(
        self,
        profile: USPBusinessProfile,
        booking_id: str,
        payment_result: USPPaymentResult,
        *,
        credential: Optional[BookingScopedCredential] = None,
        if_match: Optional[str] = None,
    ) -> Booking:
        client = await self._client(profile)
        return await client.confirm_payment(
            booking_id, payment_result, credential=credential, if_match=if_match,
        )
