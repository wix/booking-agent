"""Shared payment and booking helpers for flow commands."""
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

import httpx

from booking_agent.http_retry import with_http_retry
from booking_agent.http_trace import traced_async_client
from booking_agent.rest_client import BookingRequestError

from . import booking_questions_flow as bq_flow
from .link_cli import retrieve_spend_request
from .output import write_spend_approval_notice
from .payment_token import shared_payment_token_source
from .session import BookingRef, PaymentRef, Session
from .runtime import resolve_backend


CHECKOUT_SAFETY_MARGIN_SEC = 30


def resolve_spend_approval_url(spend_request_id: str) -> Optional[str]:
    """Fetch approval URL from link-cli. Returns URL or None."""
    try:
        sr = retrieve_spend_request(spend_request_id)
        return sr.approval_url
    except Exception:
        return None


def announce_spend_approval_url(spend_request_id: str) -> Optional[str]:
    """Fetch approval URL, write agent context to stderr, return URL."""
    approval_url = resolve_spend_approval_url(spend_request_id)
    write_spend_approval_notice(approval_url)
    return approval_url


def parse_expires_at(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        # Python 3.10's fromisoformat rejects a trailing "Z".
        expires = datetime.fromisoformat(iso[:-1] + "+00:00" if iso.endswith(("Z", "z")) else iso)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires
    except (ValueError, TypeError):
        return None


def approval_poll_deadline(session: Session, approval_timeout: int) -> float:
    import time

    now = time.time()
    deadline = now + approval_timeout
    expires = parse_expires_at(session.booking.action_expires_at)
    if expires:
        checkout_deadline = expires.timestamp() - CHECKOUT_SAFETY_MARGIN_SEC
        deadline = min(deadline, checkout_deadline)
    return deadline


async def _fetch_json(url: str) -> Dict[str, Any]:
    async def _get():
        async with traced_async_client(timeout=30.0, follow_redirects=True) as client:
            return await client.get(url)

    r = await with_http_retry(_get)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("expected JSON object")
    return data


def _slot_window(session: Session) -> Tuple[str, str, str, Optional[int]]:
    from booking_agent.ucp_checkout import iso_duration_between

    for s in session.slots_presented:
        if s.slot_id == session.slot_id:
            duration = s.duration or iso_duration_between(s.start, s.end)
            return s.start, s.end, duration, s.price_amount
    raise ValueError("selected_slot_not_in_slots_presented")


async def _create_booking_via_ucp_checkout(
    session: Session,
    service_id: str,
    notes: Optional[str],
    delivery_address: Optional[Dict[str, Any]] = None,
    *,
    reuse_checkout_id: Optional[str] = None,
    update_checkout_id: Optional[str] = None,
) -> Tuple[Any, BookingRef, str]:
    """Create the checkout, or read *reuse_checkout_id* back when it is still open.

    A create sends ``session.checkout_create_idempotency_key`` when one is saved, so a
    retry after a lost response repeats the same key. *update_checkout_id* replaces
    that open checkout's state instead (UCP Update), resending every answer.
    """
    from booking_agent.config import load_config
    from booking_agent.ucp_checkout import (
        MandatoryAcceptancePending,
        build_paid_bookings_create_body,
        build_ucp_checkout_client,
    )
    from booking_agent.ucp_negotiation import (
        intersect_ucp_capabilities,
        load_platform_profile,
        negotiate_ucp_protocol_version,
    )

    cfg = load_config()
    if not session.ucp_checkout_rest_base:
        raise RuntimeError("ucp_checkout_rest_base missing on session")
    backend = resolve_backend()
    plat_url = cfg.usp_agent_profile_url
    biz_url = session.business.profile_url or ""
    plat_doc, bus_doc = await asyncio.gather(
        load_platform_profile(plat_url),
        _fetch_json(biz_url),
    )
    ver = negotiate_ucp_protocol_version(plat_doc, bus_doc)
    # Platform-side pre-flight discovery gate: fails fast on an empty intersection.
    # NOT authoritative - the business's checkout response echo is (see below).
    platform_supported = intersect_ucp_capabilities(plat_doc, bus_doc)
    if getattr(session, "payment_route", None) == "pay_at_service":
        _require_pay_at_service_declared(platform_supported)
    _record_ucp_booking_questions(session, platform_supported, bus_doc)

    profile = await backend.resolve_profile(biz_url)
    service = await backend.get_service(profile, service_id)
    amount_info = _catalog_amount_info(session)
    line_item_amount = amount_info.get("amount_minor_units")
    slot_start, slot_end, slot_duration, slot_price = _slot_window(session)
    parts = (session.buyer.name or "Buyer").split(" ", 1)
    bf = parts[0] or "Buyer"
    bl = parts[1] if len(parts) > 1 else "Unknown"
    currency = (
        service.pricing.currency
        if service.pricing and service.pricing.currency
        else "USD"
    )
    body = build_paid_bookings_create_body(
        service=service,
        slot_id=session.slot_id,  # type: ignore[arg-type]
        slot_start=slot_start,
        slot_end=slot_end,
        slot_duration=slot_duration,
        buyer_first=bf,
        buyer_last=bl,
        buyer_email=session.buyer.email,
        currency=currency,
        slot_price_amount=slot_price,
        line_item_amount_minor=line_item_amount,
        notes=notes,
        delivery_address=delivery_address,
        booking_extras=booking_extensions(
            session, surface="paid_bookings", action=update_checkout_id is not None,
        ),
        buyer_extras=bq_flow.buyer_overrides(session) if bq_flow.is_active(session, "paid_bookings") else None,
    )
    client = build_ucp_checkout_client(
        session.ucp_checkout_rest_base,
        platform_profile_url=plat_url,
        ucp_version_header=ver,
    )
    from .session import save_session

    checkout = None
    if update_checkout_id:
        checkout = await client.update_checkout(update_checkout_id, body)
        reuse_checkout_id = update_checkout_id
    elif reuse_checkout_id:
        checkout = await _read_open_checkout(session, client, reuse_checkout_id)
        if checkout is None:
            from .session import clear_prepared_checkout

            fingerprint = session.prepared_checkout_fingerprint
            clear_prepared_checkout(session)
            session.prepared_checkout_fingerprint = fingerprint
            session.checkout_create_idempotency_key = str(uuid.uuid4())
            save_session(session)
    reused = checkout is not None
    if checkout is None:
        checkout = await client.create_checkout(
            body, idempotency_key=session.checkout_create_idempotency_key,
        )
    checkout_id = _attach_ucp_checkout_handles(session, checkout)
    if session.recipient:
        echoed = checkout.get("booking") if isinstance(checkout.get("booking"), dict) else {}
        session.recipient["echoed"] = bool(echoed.get("recipient"))
    save_session(session)
    try:
        _record_checkout_booking_questions(session, checkout, checkout_id)
        if not reused:
            _persist_checkout_issued_credential(session, client, checkout)
            save_session(session)
        return await _finalize_ucp_checkout_create(
            session,
            client,
            checkout,
            checkout_id=checkout_id,
            platform_supported=platform_supported,
            negotiated_version=ver,
            expected_amount_minor=amount_info.get("amount_minor_units"),
        )
    except MandatoryAcceptancePending:
        # The user finishes the business's own step on its page; the next buyer
        # resolve reads this checkout back instead of creating another one.
        raise
    except Exception:
        await _compensate_rejected_ucp_checkout(session, client, checkout_id)
        raise


def _catalog_amount_info(session: Session) -> Dict[str, Any]:
    """The quoted price before any checkout, even when an earlier checkout is saved."""
    import copy

    from .catalog import authoritative_amount_minor_units_for_session

    quoted = copy.copy(session)
    quoted.booking = BookingRef()
    quoted.due_now_amount_minor = None
    quoted.ucp_checkout_amount_minor = None
    quoted.ucp_merchant_quote = None
    return authoritative_amount_minor_units_for_session(quoted)


_GONE_CHECKOUT_STATUSES = frozenset({"canceled", "expired"})


async def _read_open_checkout(
    session: Session, client: Any, checkout_id: str,
) -> Optional[Dict[str, Any]]:
    """The saved checkout when the business still holds it open, else None."""
    from booking_agent.ucp_checkout import UcpCheckoutHttpError

    try:
        checkout = await client.get_checkout(
            checkout_id, credential=_checkout_presentation_credential(session),
        )
    except UcpCheckoutHttpError as e:
        if e.status in (404, 410):
            return None
        raise
    status = checkout.get("status")
    if status in _GONE_CHECKOUT_STATUSES or checkout_expired(session.prepared_checkout_expires_at):
        return None
    if status == "completed":
        raise RuntimeError(
            "The prepared checkout was already completed at the business, so it cannot be "
            "reused for a new confirmation."
        )
    return checkout


def checkout_expired(expires_at: Optional[str], *, margin_sec: int = CHECKOUT_SAFETY_MARGIN_SEC) -> bool:
    expires = parse_expires_at(expires_at)
    if expires is None:
        return False
    return expires.timestamp() - margin_sec <= datetime.now(timezone.utc).timestamp()


def _require_pay_at_service_declared(negotiated: Dict[str, Any]) -> None:
    """USP 7.6.1 fail closed: never route ``at_service`` through a checkout undeclared.

    The business declared the extension when the route was chosen; this re-checks
    it against the live profile (and the platform's own declaration) before a
    checkout is created, so a business that withdrew it cannot leave a booking
    that looks confirmed with no record of how it will be paid.
    """
    from booking_agent.namespace import CAP_PAY_AT_SERVICE
    from booking_agent.ucp_profile import CAP_PAYMENT_TERMS, UcpProfileValidationError

    missing = [c for c in (CAP_PAY_AT_SERVICE, CAP_PAYMENT_TERMS) if c not in negotiated]
    if missing:
        raise UcpProfileValidationError(
            "The business no longer declares the pay-at-service checkout capabilities "
            f"({', '.join(missing)}), so this booking cannot go through a checkout."
        )


def _attach_ucp_checkout_handles(session: Session, checkout: Dict[str, Any]) -> Optional[str]:
    """Record merchant ids before local validation so a reject is still recoverable."""
    checkout_id = checkout.get("id") or checkout.get("checkout_id")
    booking_block = (
        checkout.get("booking") if isinstance(checkout.get("booking"), dict) else {}
    )
    booking_id = str(booking_block.get("booking_id") or checkout_id or "")
    session.ucp_checkout_session_id = str(checkout_id) if checkout_id else None
    if booking_id:
        session.booking.id = booking_id
    return str(checkout_id) if checkout_id else None


def _persist_checkout_issued_credential(
    session: Session, client: Any, checkout: Dict[str, Any],
) -> None:
    from booking_agent.ucp_checkout import issued_credential_from_checkout
    from .session import record_issued_credential

    credential = issued_credential_from_checkout(
        checkout,
        own_jkt=getattr(client, "pop_jkt", None),
        required=getattr(client, "pop_jkt", None) is not None,
    )
    if credential is None:
        return
    record_issued_credential(
        session,
        credential,
        endpoint=session.ucp_checkout_rest_base or session.business.rest_endpoint,
    )


def _checkout_presentation_credential(session: Session) -> Any:
    from .session import credential_for

    if session.booking.endpoint:
        found = credential_for(session, endpoint=session.booking.endpoint)
        if found is not None:
            return found
    if session.ucp_checkout_rest_base:
        return credential_for(session, endpoint=session.ucp_checkout_rest_base)
    return None


def _persist_active_session(session: Session) -> None:
    from .session import save_session

    save_session(session)


def _safe_cancel_error(exc: BaseException) -> str:
    from booking_agent.ucp_checkout import UcpCheckoutHttpError

    if isinstance(exc, UcpCheckoutHttpError):
        return f"UCP checkout HTTP {exc.status}"
    return str(exc).replace("\n", " ").strip()[:200]


async def _compensate_rejected_ucp_checkout(
    session: Session,
    client: Any,
    checkout_id: Optional[str],
) -> None:
    session.ucp_checkout_cancel_attempted = True
    session.ucp_checkout_cancel_succeeded = False
    session.ucp_checkout_cancel_error = None
    if not checkout_id:
        session.ucp_checkout_cancel_error = "missing_checkout_id"
        _persist_active_session(session)
        return
    try:
        await client.cancel_checkout(
            checkout_id,
            credential=_checkout_presentation_credential(session),
        )
        session.ucp_checkout_cancel_succeeded = True
    except Exception as cancel_exc:
        session.ucp_checkout_cancel_succeeded = False
        session.ucp_checkout_cancel_error = _safe_cancel_error(cancel_exc)
    _persist_active_session(session)


async def cancel_ucp_checkout_async(
    session: Session,
    *,
    credential: Any = None,
) -> Dict[str, Any]:
    """Happy-path UCP checkout cancel with the persisted scoped credential."""
    from booking_agent.config import load_config
    from booking_agent.ucp_checkout import build_ucp_checkout_client

    cid = session.ucp_checkout_session_id
    if not cid or not session.ucp_checkout_rest_base:
        return {}
    presented = credential if credential is not None else _checkout_presentation_credential(session)
    cfg = load_config()
    client = build_ucp_checkout_client(
        session.ucp_checkout_rest_base,
        platform_profile_url=cfg.usp_agent_profile_url,
    )
    return await client.cancel_checkout(cid, credential=presented)


async def get_ucp_checkout_async(
    session: Session,
    *,
    credential: Any = None,
) -> Dict[str, Any]:
    """Read the persisted UCP checkout session as the merchant currently holds it."""
    from booking_agent.config import load_config
    from booking_agent.ucp_checkout import build_ucp_checkout_client

    cid = session.ucp_checkout_session_id
    if not cid or not session.ucp_checkout_rest_base:
        return {}
    presented = credential if credential is not None else _checkout_presentation_credential(session)
    cfg = load_config()
    client = build_ucp_checkout_client(
        session.ucp_checkout_rest_base,
        platform_profile_url=cfg.usp_agent_profile_url,
    )
    return await client.get_checkout(cid, credential=presented)


async def _finalize_ucp_checkout_create(
    session: Session,
    client: Any,
    checkout: Dict[str, Any],
    *,
    checkout_id: Optional[str],
    platform_supported: Dict[str, Any],
    negotiated_version: str,
    expected_amount_minor: Optional[int],
) -> Tuple[Any, BookingRef, str]:
    from booking_agent.ucp_checkout import (
        checkout_expires_at,
        extract_merchant_quote,
        extract_spt_handler_config_from_checkout,
        checkout_line_item_price_minor_units,
        checkout_subtotal_minor_units,
        checkout_total_minor_units,
        immediate_schedule_amount,
        validate_create_checkout_ready,
        validate_payment_terms,
    )
    from booking_agent.ucp_capabilities import (
        validate_checkout_capabilities,
        version_from_checkout_response,
    )

    session.prepared_checkout_expires_at = checkout_expires_at(checkout)
    validate_create_checkout_ready(checkout)
    session.ucp_merchant_quote = extract_merchant_quote(checkout)
    links = checkout.get("links")
    session.ucp_checkout_links = list(links) if isinstance(links, list) else None
    policies = checkout.get("policies")
    session.ucp_checkout_policies = list(policies) if isinstance(policies, list) else None
    messages = checkout.get("messages")
    session.ucp_checkout_messages = list(messages) if isinstance(messages, list) else None
    # Bind the negotiation to this checkout: the response echo is the authoritative
    # resolved capability set (UCP: platforms MUST defer to the response). Validate
    # it against what the platform supports, then persist it for reuse in complete.
    negotiated = validate_checkout_capabilities(checkout, platform_supported)
    session.ucp_negotiated_capabilities = negotiated
    session.ucp_protocol_version = version_from_checkout_response(checkout) or negotiated_version
    session.ucp_capabilities_authoritative = True
    slot_start = None
    booking_block = checkout.get("booking") if isinstance(checkout.get("booking"), dict) else {}
    slot = booking_block.get("slot") if isinstance(booking_block.get("slot"), dict) else {}
    if isinstance(slot, dict):
        slot_start = slot.get("start")
    require_terms = getattr(session, "payment_route", None) == "deposit_required"
    offline = getattr(session, "payment_route", None) == "pay_at_service"
    term = validate_payment_terms(
        checkout,
        negotiated_capabilities=negotiated,
        require_terms=require_terms,
        slot_start=slot_start if isinstance(slot_start, str) else None,
        expect_offline_settlement=offline,
    )
    if term is not None:
        session.selected_term_id = str(term.get("id") or "") or None
        session.payment_schedules = [
            s for s in (term.get("schedules") or []) if isinstance(s, dict)
        ]
        session.due_now_amount_minor = 0 if offline else immediate_schedule_amount(term)
        session.accepted_term = term
        if not session.complete_checkout_idempotency_key:
            import uuid as _uuid

            session.complete_checkout_idempotency_key = str(_uuid.uuid4())
    disclosures = []
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get("presentation") == "disclosure":
                disclosures.append(message)
    session.payment_disclosures = disclosures or None
    checkout_total = checkout_total_minor_units(checkout)
    if checkout_total is not None:
        session.ucp_checkout_amount_minor = checkout_total
    # Tax, fees, and discounts live in totals, so the catalog price is checked against
    # the line item (then the subtotal) and only against the total when neither is sent.
    priced, compared = checkout_line_item_price_minor_units(checkout), "line_item"
    if priced is None:
        priced, compared = checkout_subtotal_minor_units(checkout), "subtotal"
    if priced is None:
        priced, compared = checkout_total, "total"
    if (
        priced is not None
        and expected_amount_minor is not None
        and int(expected_amount_minor) != int(priced)
    ):
        from booking_agent.observability import emit_event, money_path_fields

        emit_event(
            "price_mismatch",
            phase="create",
            compared=compared,
            expected=int(expected_amount_minor),
            actual=int(priced),
            **money_path_fields(
                spend_request_id=session.payment.spend_request_id,
                booking_id=session.booking.id,
            ),
        )
        label = {"line_item": "line item price", "subtotal": "subtotal", "total": "total"}[compared]
        raise RuntimeError(
            f"The business checkout priced the service at {priced} minor units ({label}), "
            f"but the catalog price is {expected_amount_minor} minor units. Re-quote the user "
            "before requesting Link payment."
        )
    booking_id = session.booking.id or ""
    if not booking_id:
        raise RuntimeError("UCP create_checkout did not return a booking or checkout id")
    if offline:
        # Nothing is charged, so no payment handler or Link network is needed. An
        # empty handler set is expected here (USP 11.3.5 S4), not an escalation.
        handler_cfg = {}
    else:
        handler_cfg = extract_spt_handler_config_from_checkout(checkout)
        if not handler_cfg.get("network_id") and checkout_id:
            checkout_get = await client.get_checkout(str(checkout_id))
            handler_cfg = extract_spt_handler_config_from_checkout(checkout_get)
    nid = handler_cfg.get("network_id")
    session.ucp_spt_network_id = str(nid).strip() if isinstance(nid, str) and nid.strip() else None
    ref = BookingRef(
        id=booking_id,
        checkout_path="ucp",
        payment_context=None,
        action_expires_at=None,
        endpoint=session.booking.endpoint,
        credential=session.booking.credential,
        credential_expires_at=session.booking.credential_expires_at,
        credential_jkt=session.booking.credential_jkt,
        credential_scope=list(session.booking.credential_scope)
        if session.booking.credential_scope
        else None,
    )
    return SimpleNamespace(id=booking_id, booking_url=None), ref, "ucp"


PROFILE_UNRESOLVABLE_CODE = "invalid_profile_url"


def booking_attempt_fingerprint(
    service_id: str,
    slot_id: Optional[str],
    buyer: Dict[str, Any],
    delivery_address: Optional[Dict[str, Any]],
    notes: Optional[str],
) -> str:
    """A digest of what a direct ``POST /bookings`` would book, for key reuse."""
    material = json.dumps(
        [service_id, slot_id, buyer, delivery_address, notes],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _booking_idempotency_key(session: Session, fingerprint: str) -> str:
    pending = session.pending_booking_attempt
    if (
        isinstance(pending, dict)
        and pending.get("fingerprint") == fingerprint
        and isinstance(pending.get("idempotency_key"), str)
        and pending["idempotency_key"]
    ):
        return pending["idempotency_key"]
    return str(uuid.uuid4())


class CheckoutCleanupUnverified(RuntimeError):
    """A checkout the user did not complete could not be confirmed as canceled."""

    def __init__(self, message: str, *, checkout_id: Optional[str], outcome: str) -> None:
        super().__init__(message)
        self.checkout_id = checkout_id
        self.outcome = outcome


def prepared_checkout_fingerprint(
    session: Session,
    service_id: str,
    notes: Optional[str],
    delivery_address: Optional[Dict[str, Any]],
) -> str:
    """A digest of everything a prepared checkout was created for."""
    from .gates import digest

    buyer = session.buyer
    return digest({
        "business": session.business.profile_url,
        "service_id": service_id,
        "slot_id": session.slot_id,
        "route": session.payment_route,
        "buyer": [buyer.name, buyer.email, buyer.phone],
        "delivery_address": delivery_address,
        "notes": notes,
        **checkout_inputs_extra(session),
    })


def checkout_inputs_extra(session: Session) -> Dict[str, Any]:
    """Inputs beyond the core booking fields that also shape the checkout body."""
    extra: Dict[str, Any] = {}
    if bq_flow.is_active(session):
        extra["booking_questions"] = bq_flow.submissions_digest(session)
        extra["assessment_token"] = bq_flow.valid_token(session)
        extra["buyer_overrides"] = sorted(bq_flow.buyer_overrides(session).items()) or None
    if session.recipient:
        extra["recipient"] = recipient_payload(session)
    return {k: v for k, v in extra.items() if v}


def recipient_payload(session: Session) -> Optional[Dict[str, Any]]:
    """The USP ``recipient`` (Buyer shape) from what the user typed, or None."""
    r = session.recipient or {}
    if not r.get("first_name"):
        return None
    out = {"first_name": r["first_name"], "last_name": r.get("last_name") or ""}
    for key in ("email", "phone_number"):
        if r.get(key):
            out[key] = r[key]
    return out


def booking_extensions(session: Session, *, surface: str, action: bool = False) -> Dict[str, Any]:
    """Request members beyond the core booking: ``recipient`` and booking-questions fields."""
    out: Dict[str, Any] = {}
    recipient = recipient_payload(session)
    if recipient:
        out["recipient"] = recipient
    out.update(bq_flow.request_fields(session, surface=surface, action=action))
    return out


def _record_checkout_booking_questions(
    session: Session, checkout: Dict[str, Any], checkout_id: Optional[str],
) -> None:
    """Keep the business's set state and any buyer-input Action from a checkout response."""
    from booking_agent.namespace import ACTION_BOOKING_QUESTIONS_BUYER_INPUT

    from booking_agent.booking_questions import OUTCOME_CODES
    from booking_agent.rest_client import BookingQuestionsRejected

    if not bq_flow.is_active(session, "paid_bookings"):
        return
    outcomes = [
        m for m in checkout.get("messages") or []
        if isinstance(m, dict) and m.get("type") == "error" and m.get("code") in OUTCOME_CODES
    ]
    if outcomes:
        raise BookingQuestionsRejected(outcomes)
    booking = checkout.get("booking") if isinstance(checkout.get("booking"), dict) else {}
    bq_flow.record_business_state(session, booking.get("booking_question_state"))
    actions = checkout.get("actions") if isinstance(checkout.get("actions"), dict) else {}
    bq_flow.record_action(
        session, actions.get(ACTION_BOOKING_QUESTIONS_BUYER_INPUT),
        surface="paid_bookings", booking_id=checkout_id,
    )


def _record_ucp_booking_questions(
    session: Session, intersection: Dict[str, Any], business_doc: Dict[str, Any],
) -> None:
    from booking_agent.booking_questions_negotiation import ucp_active_surfaces

    active = ucp_active_surfaces(intersection, business_doc)
    st = bq_flow.state(session)
    if not st.get("surfaces"):
        return
    current = set(st["surfaces"])
    if "paid_bookings" in active:
        current.add("paid_bookings")
    else:
        current.discard("paid_bookings")
    st["surfaces"] = sorted(current)


def approval_snapshot(session: Session) -> Dict[str, Any]:
    """The business's checkout terms the summary shows, in minor units and plain text."""
    from .booking_summary import collect_provider_policies

    quote = session.ucp_merchant_quote if isinstance(session.ucp_merchant_quote, dict) else {}
    total = quote.get("total_minor_units")
    if total is None:
        total = session.ucp_checkout_amount_minor
    due_now = session.due_now_amount_minor
    balance = None
    if total is not None and due_now is not None:
        balance = max(0, int(total) - int(due_now))
    schedules = []
    for schedule in session.payment_schedules or []:
        if not isinstance(schedule, dict):
            continue
        description = schedule.get("description")
        schedules.append({
            "type": schedule.get("type"),
            "amount_minor_units": schedule.get("amount"),
            "due_at": schedule.get("due_at"),
            "text": description.get("plain") if isinstance(description, dict) else None,
        })
    policies = collect_provider_policies(session)
    return {
        "route": session.payment_route,
        "currency": quote.get("currency"),
        "line_item_minor_units": quote.get("line_item_price_minor_units"),
        "subtotal_minor_units": quote.get("subtotal_minor_units"),
        "tax_minor_units": quote.get("tax_minor_units"),
        "total_minor_units": total,
        "due_now_minor_units": due_now,
        "balance_minor_units": balance,
        "schedules": schedules,
        "policy_lines": list(policies["lines"]),
        "policy_links": list(policies["links"]),
    }


def snapshot_digest(snapshot: Optional[Dict[str, Any]]) -> Optional[str]:
    from .gates import digest

    return digest({"snapshot": snapshot}) if snapshot else None


async def prepare_ucp_checkout_async(
    session: Session,
    service_id: str,
    notes: Optional[str],
    delivery_address: Optional[Dict[str, Any]] = None,
    *,
    submit_action: bool = False,
) -> Tuple[Any, BookingRef, str]:
    """Create the checkout for the summary, or reuse the one already made for the same inputs.

    Inputs that changed cancel the old checkout first (confirmed, or
    ``CheckoutCleanupUnverified``). The create key is saved before the request.
    ``submit_action`` sends the buyer's answer to the checkout's buyer-input Action
    as an update of that same checkout.
    """
    from dataclasses import replace

    from .session import has_open_prepared_checkout, save_session

    fingerprint = prepared_checkout_fingerprint(session, service_id, notes, delivery_address)
    reuse_id = None
    update_id = None
    if submit_action and has_open_prepared_checkout(session):
        update_id = session.ucp_checkout_session_id
        session.prepared_checkout_fingerprint = fingerprint
        save_session(session)
    elif session.prepared_checkout_fingerprint == fingerprint and not session.prepared_checkout_consumed:
        reuse_id = session.ucp_checkout_session_id
    elif has_open_prepared_checkout(session):
        await abandon_checkout_async(session, reason="booking_details_changed")
    if reuse_id is None and update_id is None:
        if (
            session.prepared_checkout_fingerprint != fingerprint
            or not session.checkout_create_idempotency_key
        ):
            session.checkout_create_idempotency_key = str(uuid.uuid4())
        session.prepared_checkout_fingerprint = fingerprint
        session.prepared_checkout_consumed = False
        save_session(session)
    try:
        booking_obj, ref, path = await _create_booking_via_ucp_checkout(
            session, service_id, notes, delivery_address=delivery_address,
            reuse_checkout_id=reuse_id,
            **({"update_checkout_id": update_id} if update_id else {}),
        )
    finally:
        if session.booking.id:
            session.prepared_booking_id = session.booking.id
            session.booking.id = None
            save_session(session)
    session.prepared_booking_id = ref.id
    session.booking = replace(ref, id=None)
    session.prepared_checkout_snapshot = approval_snapshot(session)
    session.prepared_checkout_digest = snapshot_digest(session.prepared_checkout_snapshot)
    save_session(session)
    return booking_obj, ref, path


def take_prepared_checkout(session: Session, booking_ref: BookingRef) -> None:
    """Hand the prepared checkout to the pay steps with the user's yes already recorded."""
    booking_ref.id = booking_ref.id or session.prepared_booking_id
    apply_booking_to_session(session, booking_ref, "ucp")
    session.prepared_booking_id = None
    session.prepared_checkout_consumed = True
    session.disclosures_user_confirmed = True
    if session.ucp_checkout_amount_minor is not None:
        session.total_confirmed_amount_minor = int(session.ucp_checkout_amount_minor)


def _record_abandonment(
    session: Session, *, checkout_id: Optional[str], reason: str, outcome: str,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    from .session import save_session

    record = {
        "checkout_id": checkout_id,
        "reason": reason,
        "outcome": outcome,
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    session.checkout_abandonment = record
    save_session(session)
    return record


async def abandon_checkout_async(session: Session, reason: str) -> Dict[str, Any]:
    """Cancel a checkout the user did not complete, and only report it released when it is.

    ``outcome`` is ``released`` when the business answered ``status: canceled``, ``not_found``
    when it no longer has the checkout, and ``none`` when there was nothing to cancel. Local
    checkout state is cleared only for those. Anything else raises
    ``CheckoutCleanupUnverified`` and keeps local state, so the cancel can be retried.
    """
    from booking_agent.ucp_checkout import UcpCheckoutHttpError

    from .session import clear_prepared_checkout, save_session

    checkout_id = session.ucp_checkout_session_id
    if not checkout_id or not session.ucp_checkout_rest_base:
        return {"checkout_id": None, "reason": reason, "outcome": "none", "status": None}

    def _closed(outcome: str, status: Optional[str] = None) -> Dict[str, Any]:
        record = _record_abandonment(
            session, checkout_id=checkout_id, reason=reason, outcome=outcome, status=status,
        )
        clear_prepared_checkout(session)
        save_session(session)
        return record

    try:
        response = await _cancel_checkout_for_abandonment(session)
    except UcpCheckoutHttpError as e:
        if e.status in (404, 410):
            return _closed("not_found")
        closed = await _closed_checkout_status(session)
        if closed is not None:
            return _closed("already_closed", closed)
        _record_abandonment(
            session, checkout_id=checkout_id, reason=reason, outcome="failed",
            status=f"http_{e.status}",
        )
        raise CheckoutCleanupUnverified(
            f"The business did not confirm the checkout was canceled (HTTP {e.status}).",
            checkout_id=checkout_id, outcome="failed",
        ) from e
    except Exception as e:
        _record_abandonment(session, checkout_id=checkout_id, reason=reason, outcome="failed")
        raise CheckoutCleanupUnverified(
            f"The checkout cancel could not be confirmed ({_safe_cancel_error(e)}).",
            checkout_id=checkout_id, outcome="failed",
        ) from e
    status = response.get("status") if isinstance(response, dict) else None
    if status == "canceled":
        return _closed("released", status)
    if status == "expired":
        return _closed("already_closed", status)
    outcome = "already_completed" if status == "completed" else "unconfirmed"
    _record_abandonment(
        session, checkout_id=checkout_id, reason=reason, outcome=outcome, status=status,
    )
    raise CheckoutCleanupUnverified(
        "The business completed this checkout, so it was not released."
        if outcome == "already_completed"
        else f"The business answered the cancel with status {status!r}, not 'canceled'.",
        checkout_id=checkout_id, outcome=outcome,
    )


async def _cancel_checkout_for_abandonment(session: Session) -> Dict[str, Any]:
    return await cancel_ucp_checkout_async(session)


async def _closed_checkout_status(session: Session) -> Optional[str]:
    """``canceled`` or ``expired`` when the business already closed the checkout, else None."""
    try:
        checkout = await get_ucp_checkout_async(session)
    except Exception:
        return None
    status = checkout.get("status") if isinstance(checkout, dict) else None
    return status if status in _GONE_CHECKOUT_STATUSES else None


async def create_booking_async(
    session: Session,
    service_id: str,
    notes: Optional[str] = None,
    delivery_address: Optional[Dict[str, Any]] = None,
    retry_command: Optional[str] = None,
) -> Tuple[Any, BookingRef, str]:
    """Create booking and return (booking_obj, booking_ref, checkout_path).

    A direct booking the business refuses as ``invalid_profile_url`` leaves
    ``session.pending_booking_attempt`` set, so rerunning ``retry_command`` for the
    same booking replays the same ``Idempotency-Key``. Callers save the session.
    """
    if session.checkout_mode == "ucp_native" and session.ucp_checkout_rest_base:
        if getattr(session, "payment_route", None) in ("free", "at_service"):
            pass
        else:
            return await _create_booking_via_ucp_checkout(
                session, service_id, notes, delivery_address=delivery_address
            )

    backend = resolve_backend()

    profile = await backend.resolve_profile(session.business.profile_url)
    buyer = {
        "first_name": (session.buyer.name.split(" ", 1) + [""])[0] or "Buyer",
        "last_name": (session.buyer.name.split(" ", 1) + [""])[1] or "Unknown",
        "email": session.buyer.email,
    }
    if session.buyer.phone:
        buyer["phone_number"] = session.buyer.phone
    if bq_flow.is_active(session, "bookings"):
        buyer.update({k: v for k, v in bq_flow.buyer_overrides(session).items() if v})
    extensions = booking_extensions(session, surface="bookings")
    fingerprint = booking_attempt_fingerprint(
        service_id, session.slot_id, buyer, delivery_address,
        notes if not extensions else json.dumps([notes, extensions], sort_keys=True),
    )
    idempotency_key = _booking_idempotency_key(session, fingerprint)
    try:
        booking_obj = await backend.create_booking(
            profile=profile,
            service_id=service_id,
            slot_id=session.slot_id,  # type: ignore[arg-type]
            buyer=buyer,
            party_size=1,
            notes=notes,
            delivery_address=delivery_address,
            idempotency_key=idempotency_key,
            **extensions,
        )
    except BookingRequestError as e:
        session.pending_booking_attempt = (
            {
                "idempotency_key": idempotency_key,
                "fingerprint": fingerprint,
                "retry_command": retry_command,
            }
            if e.business_code == PROFILE_UNRESOLVABLE_CODE
            else None
        )
        raise
    except BaseException:
        session.pending_booking_attempt = None
        raise
    session.pending_booking_attempt = None
    if session.recipient:
        session.recipient["echoed"] = bool(getattr(booking_obj, "recipient", None))

    checkout_path = "none"
    ref = BookingRef(
        id=booking_obj.id,
        checkout_path=checkout_path,
        # The creation response is the only place a booking-scoped credential
        # ever appears, so it is captured here or lost.  ``booking_url`` is the
        # handoff target when the credential is later missing or rejected.
        endpoint=profile.rest_endpoint,
        booking_url=getattr(booking_obj, "booking_url", None),
        revision=getattr(booking_obj, "revision", None),
    )
    _attach_issued_credential(ref, backend.last_issued_credential)
    return booking_obj, ref, checkout_path


def _attach_issued_credential(ref: BookingRef, credential: Any) -> None:
    """Copy an issued credential onto the ``BookingRef`` that will be persisted."""
    if credential is None:
        return
    ref.credential = credential.value
    ref.credential_expires_at = credential.expires_at.isoformat()
    ref.credential_jkt = credential.cnf.jkt if credential.cnf else None
    ref.credential_scope = list(credential.scope) if credential.scope else None


async def complete_ucp_checkout_async(
    session: Session,
    *,
    handler_id: Optional[str] = None,
    without_instrument: bool = False,
) -> Dict[str, Any]:
    """POST complete_checkout for the session's UCP checkout (GH-002, GH-059).

    ``without_instrument`` completes a pay-at-service checkout with ``"payment": {}``
    (USP 11.3.5 S4/S5): no spend request, token, or instrument is involved, and it is
    refused unless the live checkout declares offline settlement.

    ``handler_id`` may be omitted when the checkout GET response exposes a single
    ``payment.instruments[].handler_id``, lists the published ``stripe_payments``
    instance among its handlers, or the business profile lists exactly one
    ``payment_handlers`` entry (GH-004).
    """
    from booking_agent.config import load_config
    from booking_agent.observability import emit_event, money_path_fields, payment_decline_reason_class
    from booking_agent.ucp_checkout import (
        CompleteClassification,
        CompleteOutcome,
        UcpCheckoutHttpError,
        UcpCompleteOutcomeError,
        UcpProfileValidationError,
        build_payment_instrument_for_complete_checkout,
        build_ucp_checkout_client,
        checkout_total_minor_units,
        classify_complete_checkout,
        parse_checkout_error_body,
        extract_order_id_from_checkout,
        immediate_schedule_amount,
        resolve_handler_id_for_ucp_complete,
        validate_checkout_totals_and_links,
        validate_completed_checkout_selected_term,
        validate_payment_terms,
        validate_ucp_checkout_completed,
    )

    from booking_agent.ucp_negotiation import (
        intersect_ucp_capabilities,
        load_platform_profile,
        negotiate_ucp_protocol_version,
    )
    from booking_agent.ucp_capabilities import validate_checkout_capabilities

    cid = session.ucp_checkout_session_id
    if not cid:
        raise RuntimeError("Missing ucp_checkout_session_id on session")
    if session.payment.status == "paid":
        from booking_agent.config import load_config as _load_paid
        from booking_agent.ucp_checkout import build_ucp_checkout_client as _build_paid

        cfg_paid = _load_paid()
        client_paid = _build_paid(
            session.ucp_checkout_rest_base or "",
            platform_profile_url=cfg_paid.usp_agent_profile_url,
            ucp_version_header=session.ucp_protocol_version,
        )
        replay = await client_paid.get_checkout(cid)
        return {
            "complete_checkout": replay,
            "checkout_id": cid,
            "order_id": extract_order_id_from_checkout(replay) if isinstance(replay, dict) else "",
            "replay": True,
        }
    sr_id = None if without_instrument else session.payment.spend_request_id
    spt = shared_payment_token_source().token_for(sr_id) if sr_id else None
    if not spt and not without_instrument:
        raise RuntimeError(
            "UCP complete_checkout needs a shared payment token: run "
            "`booking-agent flow pay ucp-complete --spend-request-id <id>` after the user "
            "approves the spend request (SPT credential type)."
        )
    cfg = load_config()
    plat_url = cfg.usp_agent_profile_url
    biz_url = session.business.profile_url or ""
    # Reuse the authoritative capability set persisted at create_checkout instead of
    # re-fetching the business profile: the checkout response echo already resolved
    # it for this session (GH-123). Fall back to a fresh negotiation for older or
    # resumed sessions that predate the persisted fields.
    bus_doc: Optional[Dict[str, Any]] = None
    if session.ucp_capabilities_authoritative and session.ucp_protocol_version:
        ver = session.ucp_protocol_version
        platform_supported = session.ucp_negotiated_capabilities
    else:
        plat_doc, bus_doc = await asyncio.gather(
            load_platform_profile(plat_url),
            _fetch_json(biz_url),
        )
        ver = negotiate_ucp_protocol_version(plat_doc, bus_doc)
        platform_supported = intersect_ucp_capabilities(plat_doc, bus_doc)
    client = build_ucp_checkout_client(
        session.ucp_checkout_rest_base or "",
        platform_profile_url=plat_url,
        ucp_version_header=ver,
    )
    checkout_state = await client.get_checkout(cid)
    # The live checkout response remains authoritative: re-validate its capability
    # echo before completing (defer-to-response; fail only on unsupported activation).
    validate_checkout_capabilities(checkout_state, platform_supported)
    require_terms = getattr(session, "payment_route", None) == "deposit_required"
    offline = getattr(session, "payment_route", None) == "pay_at_service"
    if without_instrument and not offline:
        raise UcpProfileValidationError(
            "A payment instrument is required: this booking was not routed as pay-at-service."
        )
    booking_block = (
        checkout_state.get("booking") if isinstance(checkout_state.get("booking"), dict) else {}
    )
    live_slot = booking_block.get("slot") if isinstance(booking_block.get("slot"), dict) else {}
    live_slot_start = live_slot.get("start") if isinstance(live_slot.get("start"), str) else None
    term = validate_payment_terms(
        checkout_state,
        negotiated_capabilities=platform_supported,
        require_terms=require_terms,
        persisted_term=session.accepted_term,
        slot_start=live_slot_start if offline else None,
        expect_offline_settlement=offline,
    )
    if term is not None and not offline:
        due_now = immediate_schedule_amount(term)
        if (
            session.due_now_amount_minor is not None
            and due_now is not None
            and int(due_now) != int(session.due_now_amount_minor)
        ):
            raise UcpProfileValidationError(
                "The immediate deposit amount changed since it was quoted."
            )
    if without_instrument:
        # USP 11.3.5 S5: a platform MUST NOT submit instruments on a checkout whose
        # settlement names the selected term. UCP requires the payment object on
        # complete, and payment.json permits it to be empty.
        resolved_handler = None
        body: Dict[str, Any] = {"payment": {}}
    else:
        resolved_handler = resolve_handler_id_for_ucp_complete(
            explicit_handler_id=handler_id,
            checkout=checkout_state,
            business_profile_doc=bus_doc,
        )
        instrument = build_payment_instrument_for_complete_checkout(
            checkout_state, resolved_handler, spt,
        )
        body = {
            "payment": {
                "instruments": [instrument],
            }
        }

    async def _complete(payload: Dict[str, Any]) -> Dict[str, Any]:
        key = session.complete_checkout_idempotency_key
        if not key:
            import uuid as _uuid

            key = str(_uuid.uuid4())
            session.complete_checkout_idempotency_key = key
        return await client.complete_checkout(cid, payload, idempotency_key=key)

    mp_fields = money_path_fields(
        spend_request_id=sr_id,
        booking_id=session.booking.id,
    )
    decline_fields = {
        "checkout_id": cid,
        "merchant_profile_url": session.business.profile_url,
        "merchant_name": session.business.name,
        "new_card_retry": bool(
            session.payment.declined_spend_request_id
            and session.payment.declined_spend_request_id != sr_id
        ),
    }

    def _outcome_error(
        classification: CompleteClassification, checkout: Dict[str, Any],
    ) -> UcpCompleteOutcomeError:
        if classification.outcome is CompleteOutcome.CAPTURED_UNCONFIRMED:
            emit_event("payment_captured_booking_unconfirmed", **mp_fields)
        return UcpCompleteOutcomeError.from_classification(
            classification, checkout_id=str(checkout.get("id") or cid),
        )

    async def _post() -> Dict[str, Any]:
        try:
            return await _complete(body)
        except UcpCheckoutHttpError as exc:
            checkout = parse_checkout_error_body(exc.body)
            classification = classify_complete_checkout(checkout)
            declined = classification.outcome is CompleteOutcome.DECLINED
            emit_event(
                "payment_declined",
                reason_class=payment_decline_reason_class(exc.status, exc.body),
                status=exc.status,
                **mp_fields,
                **(
                    {"message_code": classification.message.get("code"), **decline_fields}
                    if declined and classification.message
                    else {}
                ),
            )
            if checkout is not None and classification.outcome not in (
                CompleteOutcome.UNCLASSIFIED, CompleteOutcome.COMPLETED,
            ):
                raise _outcome_error(classification, checkout) from exc
            raise

    def _offline_awaiting_confirmation(checkout: Any) -> bool:
        # Nothing was charged, so a completed offline checkout whose booking is not
        # confirmed yet is an accepted order waiting for the business, never a capture.
        if not without_instrument or not isinstance(checkout, dict):
            return False
        if checkout.get("status") != "completed":
            return False
        booking = checkout.get("booking")
        return not (isinstance(booking, dict) and booking.get("booking_status") == "confirmed")

    def _raise_if_classified(checkout: Any) -> None:
        if not isinstance(checkout, dict) or _offline_awaiting_confirmation(checkout):
            return
        classification = classify_complete_checkout(checkout)
        if classification.outcome in (CompleteOutcome.UNCLASSIFIED, CompleteOutcome.COMPLETED):
            return
        if classification.outcome is CompleteOutcome.DECLINED and classification.message:
            emit_event(
                "payment_declined",
                reason_class="declined",
                message_code=classification.message.get("code"),
                **mp_fields,
                **decline_fields,
            )
        raise _outcome_error(classification, checkout)

    if not without_instrument:
        emit_event("charge_attempt", **mp_fields)
    result = await _post()
    # A classified outcome is never re-posted: a declined token is spent.
    _raise_if_classified(result)
    messages = result.get("messages") if isinstance(result, dict) else None
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("code") == "signal" and m.get("type") == "recoverable":
                # Single retry; the client rebuilds signals on every request.
                result = await _post()
                _raise_if_classified(result)
                break
    awaiting_confirmation = _offline_awaiting_confirmation(result)
    try:
        if awaiting_confirmation:
            validate_checkout_totals_and_links(result)
            order_id = extract_order_id_from_checkout(result)
            if not order_id:
                raise UcpProfileValidationError(
                    "UCP complete_checkout response missing order_id (or order.id)"
                )
        else:
            order_id = validate_ucp_checkout_completed(result) if isinstance(result, dict) else ""
        if term is not None and isinstance(result, dict):
            validate_completed_checkout_selected_term(result, term)
    except UcpProfileValidationError:
        if awaiting_confirmation:
            raise
        if isinstance(result, dict) and result.get("status") == "completed":
            booking_block = result.get("booking")
            booking_status = (
                booking_block.get("booking_status")
                if isinstance(booking_block, dict)
                else None
            )
            if booking_status != "confirmed":
                emit_event("payment_captured_booking_unconfirmed", **mp_fields)
        raise
    if (
        session.ucp_checkout_amount_minor is not None
        and isinstance(result, dict)
    ):
        completed_total = checkout_total_minor_units(result)
        if (
            completed_total is not None
            and int(completed_total) != int(session.ucp_checkout_amount_minor)
        ):
            emit_event(
                "price_mismatch",
                phase="complete",
                expected=int(session.ucp_checkout_amount_minor),
                actual=int(completed_total),
                **mp_fields,
            )
    if not order_id and isinstance(result, dict):
        order_id = extract_order_id_from_checkout(result)
    return {
        "complete_checkout": result,
        "checkout_id": cid,
        "order_id": order_id,
        "handler_id_resolved": resolved_handler,
        "booking_confirmation_pending": awaiting_confirmation,
    }


def create_booking_sync(
    session: Session,
    service_id: str,
    notes: Optional[str] = None,
    delivery_address: Optional[Dict[str, Any]] = None,
    retry_command: Optional[str] = None,
) -> Tuple[Any, BookingRef, str]:
    return asyncio.run(
        create_booking_async(
            session, service_id, notes, delivery_address=delivery_address,
            retry_command=retry_command,
        )
    )


def prepare_ucp_checkout_sync(
    session: Session,
    service_id: str,
    notes: Optional[str] = None,
    delivery_address: Optional[Dict[str, Any]] = None,
    *,
    submit_action: bool = False,
) -> Tuple[Any, BookingRef, str]:
    return asyncio.run(
        prepare_ucp_checkout_async(
            session, service_id, notes, delivery_address=delivery_address,
            submit_action=submit_action,
        )
    )


def abandon_checkout_sync(session: Session, reason: str) -> Dict[str, Any]:
    return asyncio.run(abandon_checkout_async(session, reason))


def apply_booking_to_session(
    session: Session,
    booking_ref: BookingRef,
    checkout_path: str,
) -> None:
    session.booking = booking_ref
    if checkout_path == "ucp":
        session.payment = PaymentRef(
            status="pending",
            spend_request_id=session.payment.spend_request_id,
        )
    else:
        session.payment = PaymentRef(status=None, spend_request_id=None)


async def resolve_spend_request_fields(session: Session) -> Dict[str, Any]:
    """Amount, currency, and line-item label for link-cli spend-request create."""
    from .catalog import authoritative_amount_minor_units_for_session

    pc = session.booking.payment_context or {}
    line_items = pc.get("lineItems") or []
    label = None
    if line_items and isinstance(line_items[0], dict):
        label = line_items[0].get("name") or line_items[0].get("label")

    amount_info = authoritative_amount_minor_units_for_session(session)
    if amount_info["amount_minor_units"] is not None:
        live_name = None
        if isinstance(session.live_service, dict):
            live_name = session.live_service.get("name")
        return {
            "amount_minor_units": int(amount_info["amount_minor_units"]),
            "currency": str(amount_info["currency"] or "USD"),
            "line_item_label": label or live_name or session.business.name or "Service",
        }

    if not session.service_id or not session.business.profile_url:
        raise RuntimeError(
            "payment_context missing on session; run `booking-agent flow book` first "
            "or ensure availability recorded service_id."
        )
    backend = resolve_backend()
    profile = await backend.resolve_profile(session.business.profile_url)
    service = await backend.get_service(profile, session.service_id)
    if not service.pricing or service.pricing.amount is None:
        raise RuntimeError("Could not resolve service price for spend request.")
    return {
        "amount_minor_units": int(service.pricing.amount),
        "currency": str(service.pricing.currency or "USD"),
        "line_item_label": service.name or session.business.name or "Service",
    }


def resolve_spend_request_fields_sync(session: Session) -> Dict[str, Any]:
    return asyncio.run(resolve_spend_request_fields(session))


def payment_path_summary(
    *,
    payment_route: Optional[str],
    checkout_path: str,
) -> str:
    if payment_route == "at_service":
        return "Pay at appointment; no digital payment required"
    if checkout_path == "ucp":
        return "UCP checkout session created; await SPT from approved spend request"
    return "Free service; no payment required"
