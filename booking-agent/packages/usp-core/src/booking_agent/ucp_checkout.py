"""UCP checkout REST client (GH-002) and response validation (GH-060 agent slice)."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx

from booking_agent.http_retry import with_http_retry
from booking_agent.http_trace import traced_async_client

from .authorization import (
    BookingScopedCredential,
    issued_credential_payload,
    parse_issued_credential,
)
from .models.wire import USPService
from .signals import with_checkout_signals
from .pop import (
    PopKey,
    PopKeyStore,
    build_rest_proof,
    credential_ath,
    dpop_nonce_from_response,
    nonce_challenge_from_401,
)
from .ucp_profile import UcpProfileValidationError

logger = logging.getLogger(__name__)

# Published Stripe Payments handler instance id (Link Agent Wallet).
_PUBLISHED_STRIPE_PAYMENTS_HANDLER_ID = "stripe_payments"


def issued_credential_from_checkout(
    checkout: Dict[str, Any],
    *,
    own_jkt: Optional[str],
    required: bool,
) -> Optional[BookingScopedCredential]:
    """Parse the creation-only ``booking_scoped_credential`` on a checkout 201."""
    payload = issued_credential_payload(checkout)
    if payload is None:
        return None
    if own_jkt is None:
        return BookingScopedCredential.model_validate(payload)
    return parse_issued_credential(payload, own_jkt=own_jkt, required=required)


class UcpCheckoutHttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"UCP checkout HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def flatten_ucp_payment_handler_entries(raw: Any) -> List[Dict[str, Any]]:
    """Normalize UCP ``payment_handlers`` whether it is a list or reverse-domain map of lists.

    UCP uses reverse-domain keys mapping to arrays of handler objects (``id``, ``version``,
    ``config``, ``available_instruments``). USP §7 examples sometimes use a flat list; both
    shapes are accepted (GH-062).
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [h for h in raw if isinstance(h, dict)]
    if isinstance(raw, dict):
        out: List[Dict[str, Any]] = []
        for val in raw.values():
            if isinstance(val, list):
                out.extend([h for h in val if isinstance(h, dict)])
            elif isinstance(val, dict):
                out.append(val)
        return out
    return []


def _handler_ids_from_flat_entries(entries: List[Dict[str, Any]]) -> List[str]:
    ids: List[str] = []
    for h in entries:
        hid = h.get("id") or h.get("handler_id") or h.get("handlerId")
        if isinstance(hid, str) and hid.strip():
            ids.append(hid.strip())
    return list(dict.fromkeys(ids))


def payment_handler_ids_from_ucp_profile_document(doc: Dict[str, Any]) -> List[str]:
    """Return handler ``id`` values from the business profile (indicative; GH-062 / GH-052).

    ``payment_handlers`` may be a list (USP shorthand) or a UCP reverse-domain map of handler
    arrays. Prefer checkout session ``payment_handlers`` at payment time when resolving
    instruments (see :func:`payment_handler_ids_from_checkout_document`).
    """
    ucp = doc.get("ucp")
    if not isinstance(ucp, dict):
        ucp = doc
    raw = ucp.get("payment_handlers") if isinstance(ucp, dict) else None
    return _handler_ids_from_flat_entries(flatten_ucp_payment_handler_entries(raw))


def payment_handler_ids_from_checkout_document(checkout: Dict[str, Any]) -> List[str]:
    """Handler ids from checkout ``ucp.payment_handlers`` or top-level ``payment_handlers``.

    Checkout response is **authoritative** for payment-time handler and instrument resolution
    (GH-062 issue #99).
    """
    raw: Any = None
    ucp = checkout.get("ucp")
    if isinstance(ucp, dict):
        raw = ucp.get("payment_handlers")
    if raw is None:
        raw = checkout.get("payment_handlers")
    return _handler_ids_from_flat_entries(flatten_ucp_payment_handler_entries(raw))


def instrument_type_hint_from_checkout_handlers(
    checkout: Dict[str, Any], handler_id: str,
) -> Optional[str]:
    """If checkout lists exactly one ``available_instruments`` type for *handler_id*, return it."""
    ucp = checkout.get("ucp")
    raw: Any = None
    if isinstance(ucp, dict):
        raw = ucp.get("payment_handlers")
    if raw is None:
        raw = checkout.get("payment_handlers")
    for h in flatten_ucp_payment_handler_entries(raw):
        hid = h.get("id") or h.get("handler_id") or h.get("handlerId")
        if not isinstance(hid, str) or hid.strip() != handler_id:
            continue
        ain = h.get("available_instruments") or h.get("availableInstruments")
        if not isinstance(ain, list) or not ain:
            return None
        types: List[str] = []
        for entry in ain:
            if isinstance(entry, str) and entry.strip():
                types.append(entry.strip())
            elif isinstance(entry, dict):
                t = entry.get("type")
                if isinstance(t, str) and t.strip():
                    types.append(t.strip())
        uniq = list(dict.fromkeys(types))
        if len(uniq) == 1:
            return uniq[0]
        return None
    return None


def _normalize_handler_config(raw: Any) -> Dict[str, Any]:
    """Normalize handler ``config`` dict; coerce ``networkId`` to ``network_id``."""
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    nid = raw.get("network_id") or raw.get("networkId")
    if isinstance(nid, str) and nid.strip():
        out["network_id"] = nid.strip()
    return out


def _network_id_from_link_instrument(handler: Dict[str, Any]) -> Optional[str]:
    """Read ``network_id`` from a handler's ``available_instruments`` ``type: link`` entry."""
    ain = handler.get("available_instruments") or handler.get("availableInstruments")
    if not isinstance(ain, list):
        return None
    for entry in ain:
        if not isinstance(entry, dict):
            continue
        t = entry.get("type")
        if not isinstance(t, str) or t.strip() != "link":
            continue
        cfg = entry.get("config")
        if not isinstance(cfg, dict):
            continue
        nid = cfg.get("network_id") or cfg.get("networkId")
        if isinstance(nid, str) and nid.strip():
            return nid.strip()
    return None


def _spt_config_from_handler_entry(handler: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve SPT inputs from one handler: Link instrument ``config`` first, then handler ``config``."""
    nid = _network_id_from_link_instrument(handler)
    if nid:
        return {"network_id": nid}
    return _normalize_handler_config(handler.get("config"))


def extract_spt_handler_config_from_checkout(
    checkout: Dict[str, Any],
    *,
    handler_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract SPT handler inputs from checkout ``payment_handlers`` (GH-064 / Stripe Agent Wallet).

    Checkout response is authoritative at payment time. Primary path (published
    ``com.stripe.payments``): ``available_instruments`` entry ``type: "link"`` then
    ``config.network_id``. Handler-level ``config.network_id`` is a compatibility fallback
    for legacy fixtures only.
    """
    ucp = checkout.get("ucp")
    raw: Any = None
    if isinstance(ucp, dict):
        raw = ucp.get("payment_handlers")
    if raw is None:
        raw = checkout.get("payment_handlers")
    entries = flatten_ucp_payment_handler_entries(raw)
    if not entries:
        return {}

    def _entry_id(h: Dict[str, Any]) -> Optional[str]:
        hid = h.get("id") or h.get("handler_id") or h.get("handlerId")
        if isinstance(hid, str) and hid.strip():
            return hid.strip()
        return None

    if handler_id and handler_id.strip():
        want = handler_id.strip()
        for h in entries:
            if _entry_id(h) == want:
                return _spt_config_from_handler_entry(h)
        return {}

    link_handlers: List[Dict[str, Any]] = []
    for h in entries:
        if _entry_id(h) and _network_id_from_link_instrument(h):
            link_handlers.append(h)

    stripe_payments = [
        h for h in link_handlers if _entry_id(h) == _PUBLISHED_STRIPE_PAYMENTS_HANDLER_ID
    ]
    if len(stripe_payments) == 1:
        return _spt_config_from_handler_entry(stripe_payments[0])
    if len(link_handlers) == 1:
        return _spt_config_from_handler_entry(link_handlers[0])
    if len(link_handlers) > 1:
        return {}

    spt_handlers: List[Dict[str, Any]] = []
    for h in entries:
        eid = _entry_id(h)
        if not eid:
            continue
        if instrument_type_hint_from_checkout_handlers(checkout, eid) == "shared_payment_token":
            spt_handlers.append(h)
    if len(spt_handlers) == 1:
        return _spt_config_from_handler_entry(spt_handlers[0])
    if len(entries) == 1:
        return _spt_config_from_handler_entry(entries[0])
    return {}


def build_payment_instrument_for_complete_checkout(
    checkout: Dict[str, Any],
    handler_id: str,
    token: str,
    *,
    credential_expires_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one ``payment.instruments[]`` entry for ``complete_checkout`` (GH-004).

    Starts from ``handler_id`` + SPT token with Stripe Agent Wallet credential shape
    (`credential.type: stripe_payment_token`), then merges ``id`` and ``type`` from a matching
    instrument on the checkout GET response when present so the payload matches the handler
    config the merchant returned on checkout (GH-004 acceptance).
    """
    cred: Dict[str, Any] = {"type": "stripe_payment_token", "token": token}
    if isinstance(credential_expires_at, str) and credential_expires_at.strip():
        cred["expires_at"] = credential_expires_at.strip()
    inst: Dict[str, Any] = {"handler_id": handler_id, "credential": cred}
    pay = checkout.get("payment")
    if isinstance(pay, dict):
        instruments = pay.get("instruments")
        if isinstance(instruments, list):
            for cand in instruments:
                if not isinstance(cand, dict):
                    continue
                hid = cand.get("handler_id") or cand.get("handlerId")
                if not isinstance(hid, str) or hid.strip() != handler_id:
                    continue
                for key in ("id", "type"):
                    v = cand.get(key)
                    if isinstance(v, str) and v.strip():
                        inst[key] = v.strip()
                break
    hint = instrument_type_hint_from_checkout_handlers(checkout, handler_id)
    if hint and "type" not in inst:
        inst["type"] = hint
    if "type" not in inst and _network_id_from_link_instrument(
        _handler_entry_for_id(checkout, handler_id) or {},
    ):
        inst["type"] = "link"
    return inst


def _handler_entry_for_id(
    checkout: Dict[str, Any], handler_id: str,
) -> Optional[Dict[str, Any]]:
    ucp = checkout.get("ucp")
    raw: Any = None
    if isinstance(ucp, dict):
        raw = ucp.get("payment_handlers")
    if raw is None:
        raw = checkout.get("payment_handlers")
    for h in flatten_ucp_payment_handler_entries(raw):
        hid = h.get("id") or h.get("handler_id") or h.get("handlerId")
        if isinstance(hid, str) and hid.strip() == handler_id:
            return h
    return None


def handler_id_from_checkout_payment(checkout: Dict[str, Any]) -> Optional[str]:
    """Pick a single ``handler_id`` from checkout ``payment.instruments`` when unambiguous.

    Prefer instruments with ``selected: true``. If several instruments disagree, return
    ``None`` so the caller can fall back to the profile list or require ``--handler-id``.
    """
    pay = checkout.get("payment")
    if not isinstance(pay, dict):
        return None
    inst = pay.get("instruments")
    if not isinstance(inst, list) or not inst:
        return None
    selected = [i for i in inst if isinstance(i, dict) and i.get("selected") is True]
    pool = selected if selected else [i for i in inst if isinstance(i, dict)]
    ids: List[str] = []
    for i in pool:
        hid = i.get("handler_id") or i.get("handlerId")
        if isinstance(hid, str) and hid.strip():
            ids.append(hid.strip())
    uniq = list(dict.fromkeys(ids))
    if len(uniq) == 1:
        return uniq[0]
    return None


def resolve_handler_id_for_ucp_complete(
    *,
    explicit_handler_id: Optional[str],
    checkout: Dict[str, Any],
    business_profile_doc: Dict[str, Any],
) -> str:
    """Resolve ``handler_id`` for ``complete_checkout`` (GH-004, GH-062).

    Order:

    1. Explicit ``--handler-id``.
    2. ``payment.instruments`` on the checkout GET response (selected instrument when marked).
    3. Checkout ``ucp.payment_handlers`` / ``payment_handlers`` (authoritative at payment time;
       GH-062): if exactly one handler ``id`` appears across flattened entries, use it.
       If several appear and one is the published Stripe Payments instance
       ``stripe_payments``, select that instance so a second handler (for example
       Google Pay) does not block Link Agent Wallet completion.
    4. Profile ``payment_handlers`` (indicative fallback when checkout did not narrow).

    Raises if still ambiguous so agents pass ``--handler-id`` explicitly.
    """
    if explicit_handler_id and explicit_handler_id.strip():
        return explicit_handler_id.strip()
    from_session = handler_id_from_checkout_payment(checkout)
    if from_session:
        return from_session
    checkout_handler_ids = payment_handler_ids_from_checkout_document(checkout)
    if len(checkout_handler_ids) == 1:
        return checkout_handler_ids[0]
    if _PUBLISHED_STRIPE_PAYMENTS_HANDLER_ID in checkout_handler_ids:
        return _PUBLISHED_STRIPE_PAYMENTS_HANDLER_ID
    profile_ids = payment_handler_ids_from_ucp_profile_document(business_profile_doc)
    if len(profile_ids) == 1:
        return profile_ids[0]
    if not checkout_handler_ids and not profile_ids:
        raise UcpProfileValidationError(
            "No handler_id on checkout (payment.instruments or checkout payment_handlers) and "
            "no payment_handlers on profile; pass --handler-id explicitly."
        )
    raise UcpProfileValidationError(
        "Could not pick a single handler_id: checkout payment_handlers/instruments and profile "
        "are ambiguous. Pass --handler-id explicitly (see GH-062: checkout is authoritative)."
    )


_TAX_TOTAL_TYPES = frozenset({"tax", "sales_tax", "vat", "gst"})


def checkout_currency(checkout: Dict[str, Any]) -> Optional[str]:
    """Return checkout currency when the merchant sent it (no default)."""
    raw = checkout.get("currency")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    order = checkout.get("order")
    if isinstance(order, dict):
        oc = order.get("currency")
        if isinstance(oc, str) and oc.strip():
            return oc.strip()
    return None


def checkout_total_minor_units(checkout: Dict[str, Any]) -> Optional[int]:
    """Return the ``total`` entry amount from a UCP checkout ``totals`` array."""
    totals = checkout.get("totals")
    if not isinstance(totals, list):
        return None
    for entry in totals:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "total" and entry.get("amount") is not None:
            return int(entry["amount"])
    return None


def checkout_subtotal_minor_units(checkout: Dict[str, Any]) -> Optional[int]:
    """Return the ``subtotal`` entry amount from a UCP checkout ``totals`` array."""
    totals = checkout.get("totals")
    if not isinstance(totals, list):
        return None
    for entry in totals:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "subtotal" and entry.get("amount") is not None:
            return int(entry["amount"])
    return None


def checkout_line_item_price_minor_units(
    checkout: Dict[str, Any], line_item_id: str = "li_1",
) -> Optional[int]:
    """``item.price`` of the booked line item: the one with ``line_item_id``, else the only one.

    The line item price excludes tax, fees, and discounts, which sit in ``totals``.
    """
    items = checkout.get("line_items")
    if not isinstance(items, list):
        return None
    items = [i for i in items if isinstance(i, dict)]
    match = next((i for i in items if i.get("id") == line_item_id), None)
    if match is None and len(items) == 1:
        match = items[0]
    if match is None:
        return None
    item = match.get("item")
    price = item.get("price") if isinstance(item, dict) else None
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        return None
    return int(price)


PAYMENT_TERMS_CAPABILITY = "dev.ucp.common.payment.terms"


def _schedule_amount(schedule: Dict[str, Any]) -> Optional[int]:
    amount = schedule.get("amount")
    if isinstance(amount, int):
        return amount
    if isinstance(amount, dict) and amount.get("amount") is not None:
        return int(amount["amount"])
    return None


def selected_payment_term(checkout: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    payment = checkout.get("payment")
    if not isinstance(payment, dict):
        return None
    selected = payment.get("selected_term_id")
    terms = payment.get("terms")
    if not isinstance(terms, list):
        return None
    matches = [t for t in terms if isinstance(t, dict) and t.get("id") == selected]
    if len(matches) != 1:
        return None
    return matches[0]


def immediate_schedule_amount(term: Dict[str, Any]) -> Optional[int]:
    schedules = term.get("schedules")
    if not isinstance(schedules, list):
        return None
    immediate = [
        s for s in schedules
        if isinstance(s, dict) and s.get("type") == "immediate"
    ]
    if len(immediate) != 1:
        return None
    amount = _schedule_amount(immediate[0])
    if amount is None or amount <= 0:
        return None
    return amount


def validate_payment_terms(
    checkout: Dict[str, Any],
    *,
    negotiated_capabilities: Optional[Dict[str, Any]] = None,
    require_terms: bool = False,
    persisted_term: Optional[Dict[str, Any]] = None,
    slot_start: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Validate UCP payment terms on a checkout. Returns the selected term or None.

    Deposits require the negotiated terms capability, exactly one selected term,
    schedules that sum to checkout total, exactly one positive immediate amount,
    and an at-service balance with due_at.
    """
    payment = checkout.get("payment") if isinstance(checkout.get("payment"), dict) else {}
    terms = payment.get("terms") if isinstance(payment, dict) else None
    has_terms = isinstance(terms, list) and len(terms) > 0
    if not has_terms:
        if require_terms:
            raise UcpProfileValidationError(
                "This deposit checkout is missing payment terms."
            )
        return None
    caps = negotiated_capabilities or {}
    if PAYMENT_TERMS_CAPABILITY not in caps:
        raise UcpProfileValidationError(
            "Checkout returned payment terms without a negotiated "
            f"{PAYMENT_TERMS_CAPABILITY} capability."
        )
    if "split_payments" in checkout:
        raise UcpProfileValidationError("split_payments is not a supported payment path.")
    if not isinstance(terms, list) or len(terms) != 1:
        raise UcpProfileValidationError("Checkout must offer exactly one selected payment term.")
    term = selected_payment_term(checkout)
    if term is None:
        raise UcpProfileValidationError(
            "Checkout payment.selected_term_id must match exactly one terms[].id."
        )
    total = checkout_total_minor_units(checkout)
    if total is None:
        raise UcpProfileValidationError("Checkout totals must include a type=total amount.")
    schedules = term.get("schedules")
    if not isinstance(schedules, list) or not schedules:
        raise UcpProfileValidationError("Selected payment term has no schedules.")
    amounts: list[int] = []
    immediate = []
    balance = []
    for schedule in schedules:
        if not isinstance(schedule, dict):
            raise UcpProfileValidationError("Payment term schedules must be objects.")
        amount = _schedule_amount(schedule)
        if amount is None:
            raise UcpProfileValidationError(
                "Each payment schedule must carry a business-computed integer amount."
            )
        amounts.append(amount)
        if schedule.get("type") == "immediate":
            immediate.append(schedule)
        else:
            balance.append(schedule)
    if sum(amounts) != int(total):
        raise UcpProfileValidationError(
            "Payment term schedule amounts must sum to the checkout total."
        )
    if len(immediate) != 1 or _schedule_amount(immediate[0]) in (None, 0) or (
        _schedule_amount(immediate[0]) or 0
    ) <= 0:
        raise UcpProfileValidationError(
            "A deposit term must have exactly one positive immediate schedule."
        )
    if (_schedule_amount(immediate[0]) or 0) >= int(total):
        raise UcpProfileValidationError(
            "The immediate schedule must be less than the checkout total."
        )
    if len(balance) != 1:
        raise UcpProfileValidationError(
            "A deposit term must have exactly one at-service balance schedule."
        )
    due_at = balance[0].get("due_at")
    if not due_at:
        raise UcpProfileValidationError("Balance schedule must set due_at to the slot start.")
    if slot_start and due_at != slot_start:
        raise UcpProfileValidationError("Balance due_at must equal the booked slot start.")
    if persisted_term is not None and persisted_term != term:
        raise UcpProfileValidationError(
            "The payment term changed since it was quoted. Do not complete this charge."
        )
    return term


def validate_completed_checkout_selected_term(
    checkout: Dict[str, Any],
    selected_term: Dict[str, Any],
) -> None:
    """Confirm the completed checkout still names the term selected before payment.

    UCP puts ``accepted_term`` on the order document. ``complete_checkout`` returns
    an order pointer (``id``, ``permalink_url``), and the checkout field that is
    already present is ``payment.selected_term_id``.
    """
    expected = selected_term.get("id") if isinstance(selected_term, dict) else None
    if not isinstance(expected, str) or not expected:
        raise UcpProfileValidationError("Selected payment term is missing an id.")
    payment = checkout.get("payment")
    selected = payment.get("selected_term_id") if isinstance(payment, dict) else None
    if not isinstance(selected, str) or not selected:
        raise UcpProfileValidationError(
            "Completed checkout is missing payment.selected_term_id."
        )
    if selected != expected:
        raise UcpProfileValidationError(
            "Completed checkout payment.selected_term_id does not match "
            "the selected payment term."
        )


def checkout_tax_minor_units(checkout: Dict[str, Any]) -> Optional[int]:
    """Return a tax-like totals line only when the merchant sent one."""
    totals = checkout.get("totals")
    if not isinstance(totals, list):
        return None
    for entry in totals:
        if not isinstance(entry, dict):
            continue
        entry_type = str(entry.get("type") or "").lower()
        if entry_type in _TAX_TOTAL_TYPES and entry.get("amount") is not None:
            return int(entry["amount"])
    return None


def extract_merchant_quote(checkout: Dict[str, Any]) -> Dict[str, Any]:
    """Copy merchant checkout totals as returned (Track A / issue #197)."""
    quote: Dict[str, Any] = {}
    currency = checkout_currency(checkout)
    if currency:
        quote["currency"] = currency
    total = checkout_total_minor_units(checkout)
    if total is not None:
        quote["total_minor_units"] = total
    tax = checkout_tax_minor_units(checkout)
    if tax is not None:
        quote["tax_minor_units"] = tax
    subtotal = checkout_subtotal_minor_units(checkout)
    if subtotal is not None:
        quote["subtotal_minor_units"] = subtotal
    line_item_price = checkout_line_item_price_minor_units(checkout)
    if line_item_price is not None:
        quote["line_item_price_minor_units"] = line_item_price
    return quote


class MandatoryAcceptancePending(UcpProfileValidationError):
    """Checkout blocked until the buyer completes merchant-mandated acceptance."""

    def __init__(
        self,
        message: str,
        *,
        continue_url: Optional[str] = None,
        links: Optional[List[Dict[str, Any]]] = None,
        policies: Optional[List[Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.continue_url = continue_url
        self.links = list(links or [])
        self.policies = list(policies or [])
        self.messages = list(messages or [])


def validate_checkout_totals_and_links(checkout: Dict[str, Any]) -> None:
    """Assert ``totals`` and ``links`` when status is ready_for_complete or completed.

    ``totals`` is a UCP checkout-level array of ``{type, amount}`` entries (e.g.
    ``subtotal``, ``total``) per the UCP checkout schema and USP section 7.4
    examples. It must be a non-empty array.

    ``links`` must be present as an array. UCP requires the key and sets no
    ``minItems``, so an empty array is valid when the merchant has no policy URLs.
    """
    status = checkout.get("status")
    if status not in ("ready_for_complete", "completed"):
        return
    if checkout.get("totals") is None:
        raise UcpProfileValidationError("UCP checkout response missing required totals")
    totals = checkout["totals"]
    if not isinstance(totals, list) or len(totals) == 0:
        raise UcpProfileValidationError("UCP checkout totals must be a non-empty array")
    links = checkout.get("links")
    if not isinstance(links, list):
        raise UcpProfileValidationError("UCP checkout response missing links array")


def _checkout_continue_url(checkout: Dict[str, Any]) -> Optional[str]:
    raw = checkout.get("continue_url")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    booking = checkout.get("booking")
    if isinstance(booking, dict):
        actions = booking.get("actions")
        if isinstance(actions, list):
            for action in actions:
                if isinstance(action, dict):
                    cu = action.get("continue_url")
                    if isinstance(cu, str) and cu.strip():
                        return cu.strip()
    return None


def _mandatory_acceptance_pending(
    checkout: Dict[str, Any],
    *,
    reason: str,
) -> MandatoryAcceptancePending:
    links = checkout.get("links")
    policies = checkout.get("policies")
    messages = checkout.get("messages")
    return MandatoryAcceptancePending(
        reason,
        continue_url=_checkout_continue_url(checkout),
        links=links if isinstance(links, list) else None,
        policies=policies if isinstance(policies, list) else None,
        messages=messages if isinstance(messages, list) else None,
    )


def validate_create_checkout_ready(checkout: Dict[str, Any]) -> None:
    """Gate after ``create_checkout`` (USP §7.5 step 4 / implementation-plan step 14).

    Per USP §7.5 step 4, a business is only expected to return ``ready_for_complete``
    when the request already included all required buyer/line-item/booking fields;
    otherwise it **MAY** return ``incomplete`` with checkout ``messages`` of severity
    ``recoverable`` or ``requires_buyer_input`` (e.g. ``actions_pending``). That is a
    normal, non-fatal response — not a failure — when the only pending
    ``booking.actions[]`` entry is of ``type: "payment"`` (i.e. checkout is simply
    waiting on the payment step that follows via ``complete_checkout``). It is only a
    hard stop when a genuine *non-payment* action (e.g. a liability waiver) is pending,
    since resolving those (USP §7.5 step 5) is not implemented here.
    """
    status = checkout.get("status")
    booking = checkout.get("booking")
    if status == "requires_escalation":
        raise _mandatory_acceptance_pending(
            checkout,
            reason=(
                "UCP checkout requires buyer review or input before payment "
                "(status requires_escalation)."
            ),
        )
    if status not in ("ready_for_complete", "incomplete"):
        raise UcpProfileValidationError(
            f"UCP create_checkout expected status 'ready_for_complete' or 'incomplete', got {status!r}"
        )
    if status == "incomplete":
        actions = booking.get("actions") if isinstance(booking, dict) else None
        non_payment_actions = [
            a for a in (actions or []) if isinstance(a, dict) and a.get("type") != "payment"
        ]
        if non_payment_actions:
            raise _mandatory_acceptance_pending(
                checkout,
                reason=(
                    "UCP create_checkout has non-payment booking.actions pending "
                    f"(USP §7.5 step 5): {non_payment_actions!r}"
                ),
            )
        messages = checkout.get("messages")
        if isinstance(messages, list):
            actions = booking.get("actions") if isinstance(booking, dict) else None
            non_payment_actions = [
                a
                for a in (actions or [])
                if isinstance(a, dict)
                and a.get("type") != "payment"
                and a.get("status") == "pending"
            ]
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                severity = str(msg.get("severity") or "").lower()
                if severity in ("requires_buyer_review", "requires_buyer_input"):
                    if non_payment_actions:
                        raise _mandatory_acceptance_pending(
                            checkout,
                            reason=(
                                "UCP checkout message requires buyer review or input "
                                f"before payment: {msg!r}"
                            ),
                        )
    validate_checkout_totals_and_links(checkout)
    if not isinstance(booking, dict) or not booking.get("booking_id"):
        raise UcpProfileValidationError(
            "UCP create_checkout response missing booking.booking_id"
        )


def extract_order_id_from_checkout(checkout: Dict[str, Any]) -> str:
    """Return ``order_id`` from a checkout document (USP #98 / UCP ``order.id`` alias)."""
    raw = checkout.get("order_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    order = checkout.get("order")
    if isinstance(order, dict):
        oid = order.get("id")
        if isinstance(oid, str) and oid.strip():
            return oid.strip()
    return ""


def validate_ucp_checkout_completed(checkout: Dict[str, Any]) -> str:
    """Assert USP §7.5 step 20 terminal fields; return ``order_id``."""
    status = checkout.get("status")
    if status != "completed":
        raise UcpProfileValidationError(
            f"UCP complete_checkout expected status 'completed', got {status!r}"
        )
    validate_checkout_totals_and_links(checkout)
    booking = checkout.get("booking")
    if not isinstance(booking, dict):
        raise UcpProfileValidationError(
            "UCP complete_checkout response missing booking object"
        )
    booking_status = booking.get("booking_status")
    if booking_status != "confirmed":
        raise UcpProfileValidationError(
            f"UCP complete_checkout expected booking.booking_status 'confirmed', "
            f"got {booking_status!r}"
        )
    order_id = extract_order_id_from_checkout(checkout)
    if not order_id:
        raise UcpProfileValidationError(
            "UCP complete_checkout response missing order_id (or order.id)"
        )
    return order_id


class CompleteOutcome(str, Enum):
    """How the agent must react to a ``complete_checkout`` response (spec #93 Track A)."""

    COMPLETED = "completed"
    CAPTURED_UNCONFIRMED = "captured_unconfirmed"
    IN_PROGRESS = "in_progress"
    MANDATORY_ACCEPTANCE = "mandatory_acceptance"
    PAYMENT_UNRECOVERABLE = "payment_unrecoverable"
    DECLINED = "declined"
    UNCLASSIFIED = "unclassified"


_CAPTURED_UNCONFIRMED_CODE = "payment_captured_booking_unconfirmed"
_IN_PROGRESS_CODES = frozenset({"complete_in_progress", "checkout_already_in_progress"})
_MANDATORY_ACCEPTANCE_CODE = "mandatory_acceptance_pending"
# Terminal for the spend request whatever the severity: today's wire shape for an
# issuer decline and for charge-time 3DS, and the E1 reason codes.
_DECLINE_CODES = frozenset({
    "payment_declined",
    "payment_authentication_unsupported",
    "payment_not_processed",
})
# Only a recoverable payment_failed is the E1 new-card decline.
_RECOVERABLE_DECLINE_CODES = frozenset({"payment_failed"})
# payment_failed with severity unrecoverable means UCP has no valid checkout
# resource left to complete. It does not say whether money moved or whether
# another payment method would work.
_UNRECOVERABLE_PAYMENT_CODE = "payment_failed"


@dataclass(frozen=True)
class CompleteClassification:
    outcome: CompleteOutcome
    message: Optional[Dict[str, Any]] = None
    continue_url: Optional[str] = None
    links: Optional[List[Dict[str, Any]]] = None
    policies: Optional[List[Dict[str, Any]]] = None
    messages: Optional[List[Dict[str, Any]]] = None


def parse_checkout_error_body(body: str) -> Optional[Dict[str, Any]]:
    """The checkout document carried by a failed complete, or None for non-JSON."""
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def classify_complete_checkout(checkout: Any) -> CompleteClassification:
    """Classify a ``complete_checkout`` body by message ``code`` and named ``severity``.

    Never decides from ``status`` being other than ``completed`` or from the
    presence of ``continue_url``: after E1 a declined checkout is still
    ``incomplete`` and still carries one. When several codes are present the
    safest wins: success, captured, in progress, acceptance, an unrecoverable
    payment failure, then decline.
    """
    if not isinstance(checkout, dict):
        return CompleteClassification(CompleteOutcome.UNCLASSIFIED)
    raw_messages = checkout.get("messages")
    messages = [
        m for m in (raw_messages if isinstance(raw_messages, list) else [])
        if isinstance(m, dict)
    ]
    links = checkout.get("links")
    policies = checkout.get("policies")

    def result(outcome: CompleteOutcome, message: Optional[Dict[str, Any]] = None):
        return CompleteClassification(
            outcome,
            message=message,
            continue_url=_checkout_continue_url(checkout),
            links=links if isinstance(links, list) else None,
            policies=policies if isinstance(policies, list) else None,
            messages=messages or None,
        )

    def first(predicate) -> Optional[Dict[str, Any]]:
        return next((m for m in messages if predicate(m)), None)

    def code_of(m: Dict[str, Any]) -> str:
        return str(m.get("code") or "")

    def severity_of(m: Dict[str, Any]) -> str:
        return str(m.get("severity") or "").lower()

    booking = checkout.get("booking")
    booking_status = booking.get("booking_status") if isinstance(booking, dict) else None
    if checkout.get("status") == "completed":
        if booking_status == "confirmed":
            return result(CompleteOutcome.COMPLETED)
        return result(CompleteOutcome.CAPTURED_UNCONFIRMED)

    for outcome, predicate in (
        (CompleteOutcome.CAPTURED_UNCONFIRMED, lambda m: code_of(m) == _CAPTURED_UNCONFIRMED_CODE),
        (CompleteOutcome.IN_PROGRESS, lambda m: code_of(m) in _IN_PROGRESS_CODES),
        (
            CompleteOutcome.MANDATORY_ACCEPTANCE,
            lambda m: code_of(m) == _MANDATORY_ACCEPTANCE_CODE,
        ),
        (
            CompleteOutcome.PAYMENT_UNRECOVERABLE,
            lambda m: code_of(m) == _UNRECOVERABLE_PAYMENT_CODE and severity_of(m) == "unrecoverable",
        ),
        (
            CompleteOutcome.DECLINED,
            lambda m: code_of(m) in _DECLINE_CODES
            or (code_of(m) in _RECOVERABLE_DECLINE_CODES and severity_of(m) == "recoverable"),
        ),
    ):
        matched = first(predicate)
        if matched is not None:
            return result(outcome, matched)
    return CompleteClassification(CompleteOutcome.UNCLASSIFIED)


class UcpCompleteOutcomeError(UcpProfileValidationError):
    """A classified, non-successful ``complete_checkout`` outcome."""

    def __init__(
        self,
        outcome: CompleteOutcome,
        *,
        code: str,
        severity: Optional[str] = None,
        content: Optional[str] = None,
        checkout_id: Optional[str] = None,
        continue_url: Optional[str] = None,
        links: Optional[List[Dict[str, Any]]] = None,
        policies: Optional[List[Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(f"UCP complete_checkout {outcome.value}: {code}")
        self.outcome = outcome
        self.code = code
        self.severity = severity
        self.content = content
        self.checkout_id = checkout_id
        self.continue_url = continue_url
        self.links = list(links or [])
        self.policies = list(policies or [])
        self.messages = list(messages or [])

    @classmethod
    def from_classification(
        cls, classification: CompleteClassification, *, checkout_id: Optional[str],
    ) -> "UcpCompleteOutcomeError":
        message = classification.message or {}
        code = str(message.get("code") or "")
        if not code and classification.outcome is CompleteOutcome.CAPTURED_UNCONFIRMED:
            code = _CAPTURED_UNCONFIRMED_CODE
        content = message.get("content")
        return cls(
            classification.outcome,
            code=code,
            severity=message.get("severity"),
            content=content if isinstance(content, str) and content.strip() else None,
            checkout_id=checkout_id,
            continue_url=classification.continue_url,
            links=classification.links,
            policies=classification.policies,
            messages=classification.messages,
        )


def validate_ucp_checkout_canceled(
    checkout: Dict[str, Any],
    *,
    expected_booking_id: str,
) -> Optional[Dict[str, Any]]:
    """Validate a UCP cancel_checkout body.

    USP §7.5 allows the business to omit ``booking`` entirely on cancel
    (the platform already holds the booking id from create). When the
    object is present, ``booking_status`` MUST NOT be ``pending``.
    """
    status = checkout.get("status")
    if status != "canceled":
        raise UcpProfileValidationError(
            f"UCP cancel_checkout expected status 'canceled', got {status!r}"
        )
    booking = checkout.get("booking")
    if not isinstance(booking, dict):
        return None
    booking_id = booking.get("booking_id")
    if booking_id != expected_booking_id:
        raise UcpProfileValidationError(
            "UCP cancel_checkout response returned booking "
            f"{booking_id!r}, expected booking {expected_booking_id!r}"
        )
    booking_status = booking.get("booking_status")
    if booking_status != "canceled":
        raise UcpProfileValidationError(
            "UCP cancel_checkout expected booking_status 'canceled', "
            f"got {booking_status!r}"
        )
    return booking


def iso_duration_between(start: str, end: str) -> str:
    """Compute ISO 8601 duration (minutes) between two RFC 3339 timestamps."""
    from datetime import datetime

    def _parse(s: str) -> datetime:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    minutes = max(0, int((_parse(end) - _parse(start)).total_seconds() // 60))
    return f"PT{minutes}M"


def resolve_line_item_price(
    service: USPService,
    *,
    slot_price_amount: Optional[int] = None,
) -> int:
    """Catalog amount, or slot amount when ``pricing.model`` is ``variable`` (USP §7.4)."""
    model = None
    if service.pricing and service.pricing.model is not None:
        model = (
            service.pricing.model.value
            if hasattr(service.pricing.model, "value")
            else str(service.pricing.model)
        )
    if model == "variable":
        if slot_price_amount is not None:
            return int(slot_price_amount)
        return 0
    if service.pricing and service.pricing.amount is not None:
        return int(service.pricing.amount)
    return 0


def build_paid_bookings_create_body(
    *,
    service: USPService,
    slot_id: str,
    slot_start: str,
    slot_end: str,
    slot_duration: str,
    buyer_first: str,
    buyer_last: str,
    buyer_email: str,
    currency: str,
    slot_price_amount: Optional[int] = None,
    line_item_id: str = "li_1",
    line_item_amount_minor: Optional[int] = None,
    notes: Optional[str] = None,
    delivery_address: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """``create_checkout`` body with ``dev.usp-protocol.services.paid_bookings`` extension (USP §7.5 step 12)."""
    if line_item_amount_minor is not None:
        price = int(line_item_amount_minor)
    else:
        price = resolve_line_item_price(service, slot_price_amount=slot_price_amount)
    title = service.name
    svc_type = service.type if isinstance(service.type, str) else str(service.type)
    booking: Dict[str, Any] = {
        "service_id": service.id,
        "service_type": svc_type,
        "slot": {
            "id": slot_id,
            "start": slot_start,
            "end": slot_end,
            "duration": slot_duration,
        },
    }
    if notes:
        booking["notes"] = notes
    if delivery_address:
        booking["delivery_address"] = delivery_address
    return {
        "currency": currency,
        "buyer": {
            "first_name": buyer_first,
            "last_name": buyer_last,
            "email": buyer_email,
        },
        "line_items": [
            {
                "id": line_item_id,
                "item": {
                    "id": service.id,
                    "title": title,
                    "price": price,
                },
                "quantity": 1,
            }
        ],
        "booking": booking,
    }


class UcpCheckoutClient:
    """Async client for UCP shopping checkout REST under *base_url*."""

    def __init__(
        self,
        base_url: str,
        *,
        platform_profile_url: Optional[str] = None,
        ucp_version_header: Optional[str] = None,
        bearer_token: Optional[str] = None,
        pop_key: Optional[PopKey] = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        # platform_profile_url is sent in UCP-Agent and fetched for negotiation.
        self._platform_profile_url = platform_profile_url
        self._ucp_version = ucp_version_header
        self._bearer_token = bearer_token
        #: Ed25519 key for ``platform_key_pop``.  Present means every checkout
        #: request carries a proof; see :func:`build_ucp_checkout_client`.
        self._pop_key = pop_key
        #: Latest non-empty ``DPoP-Nonce`` from any response (RFC 9449 rotation).
        self._dpop_nonce: Optional[str] = None

    @property
    def pop_jkt(self) -> Optional[str]:
        return self._pop_key.jkt if self._pop_key else None

    def _headers(
        self,
        *,
        method: str,
        url: str,
        idempotency_key: Optional[str] = None,
        credential: Optional[BookingScopedCredential] = None,
    ) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self._bearer_token and credential is None:
            h["Authorization"] = (
                self._bearer_token
                if self._bearer_token.lower().startswith("bearer ")
                else f"Bearer {self._bearer_token}"
            )
        if self._platform_profile_url:
            h["UCP-Agent"] = f'profile="{self._platform_profile_url}"'
        if self._ucp_version:
            h["UCP-Version"] = self._ucp_version
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        ath: Optional[str] = None
        if credential is not None:
            # Presentation: checkout cancel (and any continuation) forwards the
            # issued booking-scoped credential. acp-checkout cannot mint it; it
            # only forwards Authorization to the inner booking cancel.
            h.pop("Authorization", None)
            h["Authorization"] = f"DPoP {credential.value}"
            ath = credential_ath(credential.value)
        if self._pop_key is not None:
            # Create/get/complete stay issuance-shaped (no credential, no ath).
            # Cancel with a credential binds the proof via ``ath``.
            h["DPoP"] = build_rest_proof(
                self._pop_key,
                htm=method,
                htu=url,
                ath=ath,
                nonce=self._dpop_nonce,
            )
        return h

    async def _send(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        credential: Optional[BookingScopedCredential] = None,
        timeout: float = 60.0,
    ) -> httpx.Response:
        sent_nonce: Optional[str] = None

        async def _do() -> httpx.Response:
            nonlocal sent_nonce
            sent_nonce = self._dpop_nonce
            # Headers are rebuilt per attempt so each proof carries a fresh
            # ``jti``; a reused one is ``pop_proof_replayed``.  The idempotency
            # key is per logical call and stays put.
            headers = self._headers(
                method=method,
                url=url,
                idempotency_key=idempotency_key,
                credential=credential,
            )
            async with traced_async_client(timeout=timeout) as client:
                response = await client.request(
                    method, url, json=json_body, headers=headers,
                )
            nonce = dpop_nonce_from_response(response)
            if nonce is not None:
                self._dpop_nonce = nonce
            return response

        try:
            response = await with_http_retry(_do)
        except httpx.HTTPStatusError as exc:
            return exc.response

        if response.status_code == 401 and self._pop_key is not None:
            challenge = nonce_challenge_from_401(response)
            if challenge is not None and challenge != sent_nonce:
                # A nonce challenge is a negotiation, not a failure: retry once
                # with a fresh proof carrying it.  Bounded at one attempt so a
                # business that always challenges cannot spin the client.
                self._dpop_nonce = challenge
                logger.info("Business challenged for a proof nonce; retrying once")
                try:
                    response = await with_http_retry(_do)
                except httpx.HTTPStatusError as exc:
                    return exc.response
        return response

    async def create_checkout(
        self, body: Dict[str, Any], *, idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        url = f"{self._base}/checkout-sessions"
        logger.info("UCP POST %s", url)
        body = await with_checkout_signals(body)
        resp = await self._send("POST", url, json_body=body, idempotency_key=key)
        if resp.status_code >= 400:
            raise UcpCheckoutHttpError(resp.status_code, resp.text)
        data = resp.json()
        if isinstance(data, dict):
            validate_checkout_totals_and_links(data)
        return data if isinstance(data, dict) else {}

    async def get_checkout(
        self,
        checkout_id: str,
        *,
        credential: Optional[BookingScopedCredential] = None,
    ) -> Dict[str, Any]:
        """Read a checkout session (``GET /checkout-sessions/{id}``).

        Reads are platform-authenticated once the session is bound to a
        booking, so *credential* is presented the same way cancel presents it.
        """
        url = f"{self._base}/checkout-sessions/{checkout_id}"
        resp = await self._send("GET", url, credential=credential)
        if resp.status_code >= 400:
            raise UcpCheckoutHttpError(resp.status_code, resp.text)
        data = resp.json()
        if isinstance(data, dict):
            validate_checkout_totals_and_links(data)
        return data if isinstance(data, dict) else {}

    async def update_checkout(
        self,
        checkout_id: str,
        body: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Replace checkout session state (UCP REST ``PUT /checkout-sessions/{id}``)."""
        key = idempotency_key or str(uuid.uuid4())
        url = f"{self._base}/checkout-sessions/{checkout_id}"
        logger.info("UCP PUT %s", url)
        body = await with_checkout_signals(body)
        resp = await self._send("PUT", url, json_body=body, idempotency_key=key)
        if resp.status_code >= 400:
            raise UcpCheckoutHttpError(resp.status_code, resp.text)
        data = resp.json()
        if isinstance(data, dict):
            validate_checkout_totals_and_links(data)
        return data if isinstance(data, dict) else {}

    async def cancel_checkout(
        self,
        checkout_id: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        credential: Optional[BookingScopedCredential] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancel a checkout session (``POST /checkout-sessions/{id}/cancel``).

        When *credential* is the ``booking_scoped_credential`` from create, the
        request is presentation-shaped: ``Authorization: DPoP <value>`` and an
        ``ath``-bound proof. Create still has no credential to present.
        """
        key = idempotency_key or str(uuid.uuid4())
        url = f"{self._base}/checkout-sessions/{checkout_id}/cancel"
        logger.info("UCP POST %s", url)
        resp = await self._send(
            "POST",
            url,
            json_body=body if body is not None else {},
            idempotency_key=key,
            credential=credential,
        )
        if resp.status_code >= 400:
            raise UcpCheckoutHttpError(resp.status_code, resp.text)
        return resp.json() if resp.content else {}

    async def complete_checkout(
        self,
        checkout_id: str,
        body: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        key = idempotency_key or str(uuid.uuid4())
        url = f"{self._base}/checkout-sessions/{checkout_id}/complete"
        body = await with_checkout_signals(body)
        resp = await self._send(
            "POST", url, json_body=body, idempotency_key=key, timeout=90.0,
        )
        if resp.status_code >= 400:
            raise UcpCheckoutHttpError(resp.status_code, resp.text)
        data = resp.json() if resp.content else {}
        if isinstance(data, dict):
            validate_checkout_totals_and_links(data)
        return data if isinstance(data, dict) else {}


def build_ucp_checkout_client(
    rest_base: str,
    *,
    platform_profile_url: Optional[str] = None,
    ucp_version_header: Optional[str] = None,
    bearer_token: Optional[str] = None,
) -> UcpCheckoutClient:
    """Build a checkout client that proves possession of this agent's key.

    Checkout is the privileged half of UCP-Native booking, so it needs the same
    ``platform_key_pop`` proof as a direct ``POST /bookings`` (USP Section
    10.1.6).  The client does not negotiate a mechanism for the
    ``dev.ucp.shopping`` binding: it always presents the proof.

    The key is per checkout origin and generated on first contact, which is the
    whole of platform onboarding -- there is no registration round trip.
    """
    pop_key: Optional[PopKey] = None
    if rest_base.strip():
        # Keyed by origin, so an unresolved base would store a key under no
        # business at all -- and the request it belongs to cannot be sent anyway.
        pop_key = PopKeyStore().get_or_create(rest_base)
    return UcpCheckoutClient(
        rest_base,
        platform_profile_url=platform_profile_url,
        ucp_version_header=ucp_version_header,
        bearer_token=bearer_token,
        pop_key=pop_key,
    )
