"""Headless UCP-native E2E test orchestrator.

This is **not** the SKILL.md demonstration path (`booking-agent flow ...`). It runs the
full protocol sequence in one shot for CI and integration verification, with human
gates and session state skipped.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class E2eStepLog:
    step: str
    ok: bool
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class UcpNativeE2eError(Exception):
    def __init__(self, step: str, code: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.code = code
        self.message = message


def _log_step(logs: List[E2eStepLog], step: str, **detail: Any) -> None:
    logs.append(E2eStepLog(step=step, ok=True, detail=detail))
    logger.info("e2e step %s ok %s", step, detail)


def _fail(logs: List[E2eStepLog], step: str, code: str, message: str) -> None:
    logs.append(E2eStepLog(step=step, ok=False, error=message, detail={"code": code}))
    raise UcpNativeE2eError(step, code, message)


async def _fetch_profile_json(url: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("expected JSON object from profile URL")
    return data


async def run_ucp_native_e2e(
    *,
    registry_url: str,
    query: str,
    spt: str,
    verticals: Optional[List[str]] = None,
    calendar_skip: bool = True,
    service_index: int = 0,
    buyer_name: str = "E2E Test Buyer",
    buyer_email: str = "e2e-buyer@booking-agent.test",
    backend: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run registry → profile → catalog → availability → UCP checkout E2E test."""
    from booking_agent.backend import USPBackend
    from booking_agent.calendar.filter import filter_slots_by_busy_times
    from booking_agent.config import load_config
    from booking_agent.headless import build_search_intent, query_availability_with_quirk
    from booking_agent.ucp_checkout import (
        build_paid_bookings_create_body,
        build_payment_instrument_for_complete_checkout,
        build_ucp_checkout_client,
        resolve_handler_id_for_ucp_complete,
        validate_create_checkout_ready,
        validate_ucp_checkout_completed,
    )
    from booking_agent.ucp_negotiation import (
        intersect_ucp_capabilities,
        load_platform_profile,
        negotiate_ucp_protocol_version,
    )
    from booking_agent.ucp_profile import fetch_and_resolve_business_profile

    from booking_agent_cli.runtime import resolve_backend

    logs: List[E2eStepLog] = []
    os.environ["USP_REGISTRY_URL"] = registry_url
    usp_backend: USPBackend = backend or resolve_backend()
    cfg = load_config()
    plat_url = cfg.usp_agent_profile_url.strip()

    try:
        intent = build_search_intent(query=query, verticals=verticals)
        services = await usp_backend.search_registry(intent)
        if not services:
            _fail(logs, "registry_search", "no_results", "Registry search returned no services")
        idx = min(max(service_index, 0), len(services) - 1)
        pick = services[idx]
        service_id = pick.id
        profile_url = pick.business.usp_profile_url if pick.business else None
        if not service_id or not profile_url:
            _fail(
                logs,
                "registry_search",
                "incomplete_hit",
                "Registry hit missing service_id or business.profile_url",
            )
        _log_step(
            logs,
            "registry_search",
            service_id=service_id,
            profile_url=profile_url,
            service_name=pick.name,
        )
    except UcpNativeE2eError:
        raise
    except Exception as exc:
        _fail(logs, "registry_search", "registry_search_failed", str(exc))

    try:
        profile, uctx = await fetch_and_resolve_business_profile(profile_url)
        if uctx is None:
            _fail(
                logs,
                "profile",
                "not_ucp_native",
                f"Profile at {profile_url} is not UCP-native (missing paid_bookings capability)",
            )
        _log_step(logs, "profile", profile_url=profile_url)
    except UcpNativeE2eError:
        raise
    except Exception as exc:
        _fail(logs, "profile", "profile_fetch_failed", str(exc))

    try:
        service = await usp_backend.get_service(profile, service_id)
        _log_step(logs, "catalog", service_id=service.id, name=service.name)
    except Exception as exc:
        _fail(logs, "catalog", "catalog_fetch_failed", str(exc))

    if calendar_skip:
        _log_step(logs, "calendar_gate", state="skipped")
    else:
        _fail(
            logs,
            "calendar_gate",
            "calendar_connect_not_in_e2e",
            "Interactive calendar connect is not supported in E2E tests; use --calendar-skip",
        )

    now = datetime.now(timezone.utc)
    window_from = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).isoformat()
    window_to = (now + timedelta(days=14)).replace(
        hour=23, minute=59, second=59, microsecond=0,
    ).isoformat()

    try:
        slots = await query_availability_with_quirk(
            usp_backend, profile, service_id, window_from, window_to,
        )
        slots = filter_slots_by_busy_times(slots, [])
        if not slots:
            _fail(logs, "availability", "no_slots", "No bookable slots in E2E window")
        slot = slots[0]
        _log_step(logs, "availability", slot_id=slot.id, start=slot.start, end=slot.end)
    except UcpNativeE2eError:
        raise
    except Exception as exc:
        _fail(logs, "availability", "availability_failed", str(exc))

    checkout_id = ""
    booking_id = ""
    order_id = ""

    try:
        plat_doc, bus_doc = await __import__("asyncio").gather(
            load_platform_profile(plat_url),
            _fetch_profile_json(profile_url),
        )
        ver = negotiate_ucp_protocol_version(plat_doc, bus_doc)
        intersect_ucp_capabilities(plat_doc, bus_doc)
        rest_base = uctx.ucp_rest_endpoint()
        parts = buyer_name.split(" ", 1)
        bf = parts[0] or "Buyer"
        bl = parts[1] if len(parts) > 1 else "Unknown"
        currency = (
            service.pricing.currency
            if service.pricing and service.pricing.currency
            else "USD"
        )
        slot_price = (
            slot.pricing.amount
            if slot.pricing and slot.pricing.amount is not None
            else None
        )
        body = build_paid_bookings_create_body(
            service=service,
            slot_id=slot.id,
            slot_start=slot.start,
            slot_end=slot.end,
            slot_duration=slot.duration,
            buyer_first=bf,
            buyer_last=bl,
            buyer_email=buyer_email,
            currency=currency,
            slot_price_amount=slot_price,
        )
        client = build_ucp_checkout_client(
            rest_base,
            platform_profile_url=plat_url,
            ucp_version_header=ver,
        )
        checkout = await client.create_checkout(body)
        validate_create_checkout_ready(checkout)
        checkout_id = str(checkout.get("id") or "")
        booking_block = checkout.get("booking") if isinstance(checkout.get("booking"), dict) else {}
        booking_id = str(booking_block.get("booking_id") or "")
        _log_step(
            logs,
            "create_checkout",
            checkout_id=checkout_id,
            booking_id=booking_id,
            status=checkout.get("status"),
        )

        _log_step(logs, "acquire_spt", source="caller")
        checkout_state = await client.get_checkout(checkout_id)
        handler_id = resolve_handler_id_for_ucp_complete(
            explicit_handler_id=None,
            checkout=checkout_state,
            business_profile_doc=bus_doc,
        )
        instrument = build_payment_instrument_for_complete_checkout(
            checkout_state, handler_id, spt,
        )
        payload: Dict[str, Any] = {"payment": {"instruments": [instrument]}}
        result = await client.complete_checkout(checkout_id, payload)
        order_id = validate_ucp_checkout_completed(result)
        _log_step(
            logs,
            "complete_checkout",
            status=result.get("status"),
            booking_status=(result.get("booking") or {}).get("booking_status"),
            order_id=order_id,
        )
    except UcpNativeE2eError:
        raise
    except Exception as exc:
        _fail(logs, "complete_checkout", "complete_checkout_failed", str(exc))

    return {
        "ok": True,
        "service_id": service_id,
        "profile_url": profile_url,
        "checkout_id": checkout_id,
        "booking_id": booking_id,
        "order_id": order_id,
        "steps": [log.__dict__ for log in logs],
    }


def emit_e2e_result(payload: Dict[str, Any], *, ok: bool) -> None:
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    if not ok:
        raise SystemExit(1)
