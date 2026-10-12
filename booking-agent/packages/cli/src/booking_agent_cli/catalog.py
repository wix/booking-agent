"""Live USP catalog helpers (USP §6.3 authoritative service at booking time)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from booking_agent.backend import CURRENCY_SYMBOLS, minor_to_decimal
from booking_agent.models.platform import RankSignals
from booking_agent.models.wire import USPService


PRE_CHECKOUT_QUOTE_CAVEAT = (
    "Catalog or registry estimate only; the charge amount is set at merchant checkout."
)


def _format_price_display(amount_minor: Optional[int], currency: Optional[str]) -> Optional[str]:
    if amount_minor is None or not currency:
        return None
    value = minor_to_decimal(int(amount_minor), currency)
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), "")
    if symbol:
        return f"{symbol}{value:.2f}"
    return f"{value:.2f} {currency.upper()}"


def _minor_units_from_registry_pricing_dict(
    pricing: Dict[str, Any],
) -> Optional[int]:
    minor = pricing.get("amountMinorUnits")
    if minor is None:
        minor = pricing.get("amount_minor_units")
    if minor is not None:
        return int(minor)
    if pricing.get("amount") is not None:
        return int(pricing["amount"])
    return None


def _minor_units_from_registry_search_result(
    result: Optional[Dict[str, Any]],
) -> Optional[int]:
    """Minor currency units from the raw registry search row."""
    if not result:
        return None
    for key in ("pricing", "price"):
        pricing = result.get(key)
        if isinstance(pricing, dict):
            minor = _minor_units_from_registry_pricing_dict(pricing)
            if minor is not None:
                return minor
    return None


def _discover_row_for_service(
    session: Any,
    service_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not service_id:
        return None
    for row in getattr(session, "discover_services", None) or []:
        if row.get("service_id") == service_id:
            return row
    return None


def _live_catalog_amount_minor_units(session: Any) -> Optional[int]:
    live = getattr(session, "live_service", None)
    if not isinstance(live, dict):
        return None
    pricing = live.get("pricing")
    if not isinstance(pricing, dict):
        return None
    minor = pricing.get("amount_minor_units")
    if minor is None:
        minor = pricing.get("amount")
    if minor is not None:
        return int(minor)
    return None


def _registry_amount_minor_units_for_session(session: Any) -> Optional[int]:
    service_id = getattr(session, "service_id", None)
    row = _discover_row_for_service(session, service_id)
    if row is None:
        return None
    return _minor_units_from_registry_search_result(row.get("registry_search_result"))


def pricing_consistency_for_session(session: Any) -> Dict[str, Any]:
    """Compare registry discover pricing with the live catalog snapshot on the session."""
    registry_minor = _registry_amount_minor_units_for_session(session)
    live_minor = _live_catalog_amount_minor_units(session)
    currency = "USD"
    row = _discover_row_for_service(session, getattr(session, "service_id", None))
    if row and row.get("currency"):
        currency = str(row["currency"])
    elif isinstance(getattr(session, "live_service", None), dict):
        live_pricing = session.live_service.get("pricing") or {}
        if live_pricing.get("currency"):
            currency = str(live_pricing["currency"])

    registry_display = None
    if row:
        registry_display = row.get("price_display") or _price_display_from_registry_search_result(
            row.get("registry_search_result"),
        )
    live_display = None
    live = getattr(session, "live_service", None)
    if isinstance(live, dict):
        pricing = live.get("pricing") or {}
        live_display = pricing.get("price_display") or _format_price_display(
            live_minor,
            pricing.get("currency") or currency,
        )

    match = (
        registry_minor is not None
        and live_minor is not None
        and registry_minor == live_minor
    )
    return {
        "registry_amount_minor_units": registry_minor,
        "live_catalog_amount_minor_units": live_minor,
        "registry_price_display": registry_display,
        "live_catalog_price_display": live_display,
        "currency": currency,
        "match": match if registry_minor is not None and live_minor is not None else None,
        "mismatch": (
            registry_minor is not None
            and live_minor is not None
            and registry_minor != live_minor
        ),
    }


def authoritative_amount_minor_units_for_session(
    session: Any,
    *,
    payment_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Minor-unit amount for charging (link-cli spend request, UCP checkout line items).

    Prefers redirect ``payment_context.amountDue``, then checkout totals returned by
    ``create_checkout``, then live catalog on UCP-native paths (what the merchant bills),
    then registry discover pricing, then ``live_service`` snapshot.
    """
    pc = payment_context
    if pc is None and getattr(session, "booking", None) is not None:
        pc = session.booking.payment_context
    if isinstance(pc, dict) and pc.get("amountDue") is not None and pc.get("currency"):
        amount = int(pc["amountDue"])
        currency = str(pc["currency"])
        return {
            "amount_minor_units": amount,
            "currency": currency,
            "amount_source": "payment_context",
            "price_display": _format_price_display(amount, currency),
        }

    due_now = getattr(session, "due_now_amount_minor", None)
    if due_now is not None:
        currency = None
        merchant_quote = getattr(session, "ucp_merchant_quote", None) or {}
        if merchant_quote.get("currency"):
            currency = str(merchant_quote["currency"])
        else:
            live = getattr(session, "live_service", None)
            if isinstance(live, dict):
                pricing = live.get("pricing") or {}
                if pricing.get("currency"):
                    currency = str(pricing["currency"])
        currency = currency or "USD"
        return {
            "amount_minor_units": int(due_now),
            "currency": currency,
            "amount_source": "payment_term_immediate",
            "price_display": _format_price_display(int(due_now), currency),
        }

    checkout_minor = getattr(session, "ucp_checkout_amount_minor", None)
    merchant_quote = getattr(session, "ucp_merchant_quote", None) or {}
    if checkout_minor is not None:
        currency = merchant_quote.get("currency")
        if not currency:
            live = getattr(session, "live_service", None)
            if isinstance(live, dict):
                pricing = live.get("pricing") or {}
                if pricing.get("currency"):
                    currency = str(pricing["currency"])
        if not currency:
            currency = None
        if currency is None:
            return {
                "amount_minor_units": int(checkout_minor),
                "currency": None,
                "amount_source": "ucp_checkout_total",
                "price_display": None,
            }
        return {
            "amount_minor_units": int(checkout_minor),
            "currency": currency,
            "amount_source": "ucp_checkout_total",
            "price_display": _format_price_display(int(checkout_minor), currency),
        }

    if getattr(session, "checkout_mode", None) == "ucp_native":
        live_minor = _live_catalog_amount_minor_units(session)
        if live_minor is not None:
            currency = None
            live = getattr(session, "live_service", None)
            if isinstance(live, dict):
                pricing = live.get("pricing") or {}
                if pricing.get("currency"):
                    currency = str(pricing["currency"])
                price_display = pricing.get("price_display") or (
                    _format_price_display(live_minor, currency) if currency else None
                )
            else:
                price_display = _format_price_display(live_minor, currency) if currency else None
            return {
                "amount_minor_units": live_minor,
                "currency": currency,
                "amount_source": "live_catalog_ucp",
                "price_display": price_display,
            }

    service_id = getattr(session, "service_id", None)
    row = _discover_row_for_service(session, service_id)
    if row is not None:
        registry_minor = _minor_units_from_registry_search_result(
            row.get("registry_search_result"),
        )
        currency = row.get("currency")
        if registry_minor is not None and currency:
            return {
                "amount_minor_units": registry_minor,
                "currency": currency,
                "amount_source": "discover_registry",
                "price_display": row.get("price_display")
                or _format_price_display(registry_minor, currency),
            }

    live = getattr(session, "live_service", None)
    if isinstance(live, dict):
        pricing = live.get("pricing")
        if isinstance(pricing, dict) and pricing.get("amount_minor_units") is not None:
            amount = int(pricing["amount_minor_units"])
            currency = str(pricing.get("currency") or "") or None
            if currency:
                return {
                    "amount_minor_units": amount,
                    "currency": currency,
                    "amount_source": "live_catalog",
                    "price_display": pricing.get("price_display")
                    or _format_price_display(amount, currency),
                }

    return {
        "amount_minor_units": None,
        "currency": None,
        "amount_source": None,
        "price_display": None,
    }


def _price_display_from_registry_search_result(
    result: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Human-readable price from the raw registry search row."""
    if not result:
        return None
    for key in ("pricing", "price"):
        pricing = result.get(key)
        if not isinstance(pricing, dict):
            continue
        display = pricing.get("display") or pricing.get("price_display")
        if display:
            return str(display)
        minor = pricing.get("amountMinorUnits")
        if minor is None:
            minor = pricing.get("amount_minor_units")
        if minor is not None:
            return _format_price_display(int(minor), pricing.get("currency"))
        if pricing.get("amount") is not None:
            return _format_price_display(
                int(pricing["amount"]),
                pricing.get("currency"),
            )
    return None


def availability_hint_summary(service: USPService) -> Optional[Dict[str, Any]]:
    """Serialize the registry/catalog availability hint for agents and support reports."""
    hint = service.availability_hint
    if hint is None or not hint.summary:
        return None
    return hint.model_dump(exclude_none=True)


def sort_discover_summaries_by_rank(
    summaries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return discover rows in registry relevance order (lowest ``search_rank`` first)."""
    return [
        summary
        for _idx, summary in sorted(
            enumerate(summaries),
            key=lambda pair: (
                pair[1].get("search_rank")
                if pair[1].get("search_rank") is not None
                else pair[0],
                pair[0],
            ),
        )
    ]


SOURCE_LIVE_CATALOG = "live_catalog"
SOURCE_REGISTRY = "registry"
DISPLAY_FIELDS = ("price", "duration", "channel", "locations")


def duration_minutes(value: Optional[str]) -> Optional[int]:
    """Whole minutes from an ISO 8601 duration such as ``PT1H30M``; None when unparseable."""
    import re

    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?", value.strip().upper())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(part or 0) for part in match.groups())
    return days * 1440 + hours * 60 + minutes


def _registry_snapshot(result: Dict[str, Any]) -> Dict[str, Any]:
    pricing = result.get("pricing") or result.get("price") or {}
    snapshot: Dict[str, Any] = {
        "service_name": result.get("serviceName") or result.get("service_name"),
        "price_minor_units": _minor_units_from_registry_search_result(result),
        "price_display": _price_display_from_registry_search_result(result),
        "currency": pricing.get("currency") if isinstance(pricing, dict) else None,
        "duration_minutes": result.get("durationMinutes") or result.get("duration_minutes"),
    }
    if isinstance(result.get("location"), dict):
        snapshot["location"] = result["location"]
    return {k: v for k, v in snapshot.items() if v is not None}


def _discovery_mismatches(
    registry: Dict[str, Any],
    *,
    service_name: Optional[str],
    price_minor: Optional[int],
    price_display: Optional[str],
    duration: Optional[str],
) -> List[Dict[str, Any]]:
    """Registry versus live catalog differences on fields the user decides on."""
    mismatches: List[Dict[str, Any]] = []
    reg_minor = registry.get("price_minor_units")
    if reg_minor is not None and price_minor is not None and int(reg_minor) != int(price_minor):
        mismatches.append({
            "field": "price",
            "registry": registry.get("price_display") or reg_minor,
            "live_catalog": price_display or price_minor,
        })
    reg_minutes = registry.get("duration_minutes")
    live_minutes = duration_minutes(duration)
    if reg_minutes is not None and live_minutes is not None and int(reg_minutes) != live_minutes:
        mismatches.append({
            "field": "duration",
            "registry": f"{int(reg_minutes)} minutes",
            "live_catalog": f"{live_minutes} minutes",
        })
    reg_name = registry.get("service_name")
    if reg_name and service_name and reg_name.strip().casefold() != service_name.strip().casefold():
        mismatches.append({
            "field": "service_name",
            "registry": reg_name,
            "live_catalog": service_name,
        })
    return mismatches


def duration_display(iso: Any) -> Optional[str]:
    """``PT1H30M`` -> ``1 hr 30 min``. None for anything that is not a simple time duration."""
    import re

    if not isinstance(iso, str):
        return None
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso.strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    minutes += (hours * 60) + (1 if seconds >= 30 else 0)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hr")
    if minutes:
        parts.append(f"{minutes} min")
    return " ".join(parts) or None


def channel_type(service_data: Optional[Dict[str, Any]]) -> Optional[str]:
    channel = (service_data or {}).get("channel")
    value = channel.get("type") if isinstance(channel, dict) else None
    return str(getattr(value, "value", value)) if value else None


def business_location_address(service_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """First catalog location's postal address, falling back to its name."""
    for loc in (service_data or {}).get("locations") or []:
        if not isinstance(loc, dict):
            continue
        text = loc.get("address") or loc.get("name")
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())
    return None


def where_text(service_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Where the service happens, in plain words, for search rows."""
    kind = channel_type(service_data)
    facts = (service_data or {}).get("location_facts") or {}
    distance = facts.get("distance_km") if isinstance(facts, dict) else None
    if kind == "at_buyer_location":
        return "comes to your address"
    if kind in ("virtual", "phone"):
        return "online" if kind == "virtual" else "by phone"
    if kind == "hybrid":
        return "at the business or online"
    if kind == "at_business_location" or (service_data or {}).get("locations"):
        place = business_location_address(service_data)
        text = f"at the business, {place}" if place else "at the business"
        if distance is not None:
            text += f", {distance:g} km away"
        return text
    return None


def discover_display_line(summary: Dict[str, Any]) -> str:
    """One line per search result: service, business, price, duration, and where."""
    service = summary.get("service_name") or "Service"
    business = summary.get("business_name")
    head = f"{service} by {business}" if business else service
    parts = [head]
    for value in (
        summary.get("price_display"),
        duration_display(summary.get("duration")),
        where_text(summary),
    ):
        if value:
            parts.append(value)
    return ", ".join(parts)


def discover_summary_from_service(service: USPService) -> Dict[str, Any]:
    """Serialize one discover/search result row for agents and support reports.

    When the business's live catalog was fetched, every displayed field comes from it.
    The registry row is kept for ranking and as ``registry_snapshot``, and any
    disagreement on a decision field is listed in ``discovery_mismatches``.
    """
    from .geo_eligibility import location_block_from_service

    meta = service.metadata or {}
    registry_result = meta.get("registry_search_result")
    catalog_source = meta.get("catalog_source", "live")
    is_live = catalog_source == "live"
    pricing = service.pricing
    currency = pricing.currency if pricing else None
    live_minor = pricing.amount if pricing else None
    if is_live:
        amount_minor = live_minor
        if amount_minor is None and pricing is not None and str(pricing.model) == "free":
            amount_minor = 0
        price_display = _format_price_display(amount_minor, currency)
        if price_display is None and isinstance(registry_result, dict):
            amount_minor = _minor_units_from_registry_search_result(registry_result)
            price_display = _price_display_from_registry_search_result(registry_result)
            price_source = SOURCE_REGISTRY
        else:
            price_source = SOURCE_LIVE_CATALOG
    else:
        registry_minor = _minor_units_from_registry_search_result(registry_result)
        amount_minor = registry_minor if registry_minor is not None else live_minor
        price_display = (
            _price_display_from_registry_search_result(registry_result)
            or _format_price_display(amount_minor, currency)
        )
        price_source = SOURCE_REGISTRY
    duration = service.duration.fixed if service.duration else None
    other_source = SOURCE_LIVE_CATALOG if is_live else SOURCE_REGISTRY
    field_sources = {field: other_source for field in DISPLAY_FIELDS}
    field_sources["price"] = price_source
    summary = {
        "service_id": service.id,
        "service_name": service.name,
        "business_name": (service.business.name if service.business else None),
        "profile_url": (service.business.usp_profile_url if service.business else None),
        "price_minor_units": amount_minor,
        "price_display": price_display,
        "currency": currency,
        "duration": duration,
        "catalog_source": catalog_source,
        "profile_resolved": meta.get("profile_resolved", True),
        "field_sources": field_sources,
        "registry_search_result": meta.get("registry_search_result"),
        "search_rank": meta.get("search_rank"),
        **location_block_from_service(service),
    }
    if isinstance(registry_result, dict):
        snapshot = _registry_snapshot(registry_result)
        if snapshot:
            summary["registry_snapshot"] = snapshot
        if is_live:
            mismatches = _discovery_mismatches(
                snapshot,
                service_name=service.name,
                price_minor=amount_minor if price_source == SOURCE_LIVE_CATALOG else None,
                price_display=price_display,
                duration=duration,
            )
            if mismatches:
                summary["discovery_mismatches"] = mismatches
    hint = availability_hint_summary(service)
    if hint is not None:
        summary["availability_hint"] = hint
    if isinstance(registry_result, dict):
        rank_signals = registry_result.get("rankSignals")
        if rank_signals is None:
            rank_signals = registry_result.get("rank_signals")
        if rank_signals is not None:
            summary["rank_signals"] = RankSignals.model_validate(
                rank_signals
            ).model_dump()
    return summary


def presented_service_changes(
    presented: Optional[Dict[str, Any]],
    live: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Decision fields that differ between the search row the user picked and the live catalog."""
    if not isinstance(presented, dict):
        return []
    changes: List[Dict[str, Any]] = []
    live_pricing = live.get("pricing") or {}
    shown_minor = presented.get("price_minor_units")
    live_minor = live_pricing.get("amount_minor_units")
    if live_minor is None and live_pricing.get("model") == "free":
        live_minor = 0
    if shown_minor is not None and live_minor is not None and int(shown_minor) != int(live_minor):
        changes.append({
            "field": "price",
            "presented": presented.get("price_display") or shown_minor,
            "live_catalog": live_pricing.get("price_display") or live_minor,
        })
    shown_channel = (presented.get("channel") or {}).get("type")
    live_channel = (live.get("channel") or {}).get("type")
    if shown_channel and live_channel and shown_channel != live_channel:
        changes.append({"field": "channel", "presented": shown_channel, "live_catalog": live_channel})
    shown_minutes = duration_minutes(presented.get("duration"))
    live_minutes = duration_minutes(live.get("duration"))
    if shown_minutes is not None and live_minutes is not None and shown_minutes != live_minutes:
        changes.append({
            "field": "duration",
            "presented": f"{shown_minutes} minutes",
            "live_catalog": f"{live_minutes} minutes",
        })
    shown_name = presented.get("service_name")
    live_name = live.get("name")
    if shown_name and live_name and shown_name.strip().casefold() != live_name.strip().casefold():
        changes.append({"field": "service_name", "presented": shown_name, "live_catalog": live_name})
    return changes


def checkout_total_changed(quote: Optional[Dict[str, Any]]) -> bool:
    """True when the checkout total differs from the quoted service price.

    ``create_checkout`` has already checked the line item (or subtotal) against the
    quoted catalog price, so a different total means merchant tax, fees, or discounts.
    """
    if not isinstance(quote, dict):
        return False
    total = quote.get("total_minor_units")
    priced = quote.get("line_item_price_minor_units")
    if priced is None:
        priced = quote.get("subtotal_minor_units")
    if total is None or priced is None:
        return False
    return int(total) != int(priced)


def merchant_quote_for_session(session: Any) -> Optional[Dict[str, Any]]:
    """Merchant checkout quote persisted after ``create_checkout``."""
    quote = getattr(session, "ucp_merchant_quote", None)
    if not isinstance(quote, dict) or not quote:
        return None
    out = dict(quote)
    currency = out.get("currency")
    total = out.get("total_minor_units")
    if total is not None and currency:
        out["total_display"] = _format_price_display(int(total), str(currency))
    tax = out.get("tax_minor_units")
    if tax is not None and currency:
        out["tax_display"] = _format_price_display(int(tax), str(currency))
    subtotal = out.get("subtotal_minor_units")
    if subtotal is not None and currency:
        out["subtotal_display"] = _format_price_display(int(subtotal), str(currency))
    line_item = out.get("line_item_price_minor_units")
    if line_item is not None and currency:
        out["line_item_display"] = _format_price_display(int(line_item), str(currency))
    out["total_changed"] = checkout_total_changed(quote)
    links = getattr(session, "ucp_checkout_links", None)
    if links:
        out["links"] = list(links)
    policies = getattr(session, "ucp_checkout_policies", None)
    if policies:
        out["policies"] = list(policies)
    messages = getattr(session, "ucp_checkout_messages", None)
    if messages:
        out["messages"] = list(messages)
    return out


def payment_price_for_session(
    session: Any,
    *,
    payment_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Authoritative human-readable price for payment confirmation.

    Uses ``authoritative_amount_minor_units_for_session`` (live catalog on UCP-native
    checkout when present). Agents must use ``price_display`` and never treat
    ``amount_minor_units`` as major currency.
    """
    info = authoritative_amount_minor_units_for_session(
        session,
        payment_context=payment_context,
    )
    consistency = pricing_consistency_for_session(session)
    merchant_quote = merchant_quote_for_session(session)
    if info["amount_minor_units"] is not None:
        result = {
            "price_display": info["price_display"],
            "currency": info["currency"],
            "amount_minor_units": info["amount_minor_units"],
            "price_source": info["amount_source"],
            "pricing_consistency": consistency,
        }
        if info["amount_source"] != "ucp_checkout_total":
            result["quote_caveat"] = PRE_CHECKOUT_QUOTE_CAVEAT
        if merchant_quote is not None:
            result["merchant_quote"] = merchant_quote
        return result

    result = {
        "price_display": None,
        "currency": None,
        "amount_minor_units": None,
        "price_source": None,
        "pricing_consistency": consistency,
    }
    if merchant_quote is not None:
        result["merchant_quote"] = merchant_quote
    return result


DIRECT_BOOKING_ROUTES = frozenset({"free", "at_service"})
CHECKOUT_BOOKING_ROUTES = frozenset({"at_booking", "deposit_required"})


def classify_payment_route(live_service: Dict[str, Any]) -> Dict[str, Any]:
    """Classify a live service into a per-service payment route.

    Timing is a service property, not a business property. Mixed catalogs
    (at_booking, at_service, deposit_required, free on one profile) must route
    each service independently.
    """
    policies = live_service.get("policies") if isinstance(live_service, dict) else None
    pricing = live_service.get("pricing") if isinstance(live_service, dict) else None
    if not isinstance(policies, dict):
        policies = {}
    if not isinstance(pricing, dict):
        pricing = {}

    requires_payment = policies.get("requires_payment")
    model = pricing.get("model")
    timing = policies.get("payment_timing")

    if requires_payment is False or model == "free":
        if timing not in (None, ""):
            return {
                "route": None,
                "timing": timing,
                "refusal": {
                    "code": "payment_timing_not_supported",
                    "message": (
                        "This free service declares payment timing, which is contradictory "
                        "and cannot be booked through this flow."
                    ),
                },
            }
        return {"route": "free", "timing": None, "refusal": None}

    if requires_payment is not True:
        return {
            "route": None,
            "timing": timing,
            "refusal": {
                "code": "payment_timing_not_supported",
                "message": (
                    "This service does not declare whether payment is required, so it "
                    "cannot be booked through this flow."
                ),
            },
        }

    if timing in (None, ""):
        return {
            "route": None,
            "timing": None,
            "refusal": {
                "code": "payment_timing_not_supported",
                "message": (
                    "This paid service does not declare payment timing, so it cannot "
                    "be booked through this flow."
                ),
            },
        }
    if timing == "at_booking":
        return {"route": "at_booking", "timing": timing, "refusal": None}
    if timing == "at_service":
        return {"route": "at_service", "timing": timing, "refusal": None}
    if timing == "deposit_required":
        return {
            "route": "deposit_required",
            "timing": timing,
            "refusal": None,
        }
    return {
        "route": None,
        "timing": timing,
        "refusal": {
            "code": "payment_timing_not_supported",
            "message": (
                f"This service uses unsupported payment timing {timing!r} and cannot be "
                "booked through this flow."
            ),
        },
    }


def payment_timing_refusal(
    live_service: Dict[str, Any],
    *,
    allow_deposits: bool = True,
) -> Optional[Dict[str, str]]:
    """Return a fail-closed error when this service cannot be booked."""
    classified = classify_payment_route(live_service)
    if classified["refusal"] is not None:
        return classified["refusal"]
    if classified["route"] == "deposit_required" and not allow_deposits:
        return {
            "code": "deposit_not_supported",
            "message": (
                "This service requires a deposit, which cannot be collected through "
                "this booking flow."
            ),
        }
    return None


def payment_timing_changed_refusal(
    session: Any,
    live_service: Dict[str, Any],
) -> Optional[Dict[str, str]]:
    """Fail closed when a later live read disagrees with the discovered timing."""
    classified = classify_payment_route(live_service)
    previous = getattr(session, "payment_timing", None)
    previous_route = getattr(session, "payment_route", None)
    new_timing = classified["timing"]
    new_route = classified["route"]
    if previous is None and previous_route is None:
        return None
    if previous == new_timing and previous_route == new_route:
        return None
    return {
        "code": "payment_timing_changed",
        "message": (
            "This service's payment timing changed since it was quoted. Get a new "
            "quote and a fresh approval before booking."
        ),
    }


def live_service_summary(service: USPService) -> Dict[str, Any]:
    """Serialize fields agents need before slot selection (registry snapshot is non-authoritative)."""
    from .geo_eligibility import location_block_from_service

    def _cancellation_policy_summary(pol) -> Optional[Dict[str, Any]]:
        cancellation = getattr(pol, "cancellation", None) if pol else None
        if cancellation is None:
            return None
        return {
            "allowed": cancellation.allowed,
            "free_cancellation_until": cancellation.free_cancellation_until or None,
            "late_cancellation_fee": cancellation.late_cancellation_fee,
            "no_cancellation_after": cancellation.no_cancellation_after or None,
        }

    def _rescheduling_policy_summary(pol) -> Optional[Dict[str, Any]]:
        rescheduling = getattr(pol, "rescheduling", None) if pol else None
        if rescheduling is None:
            return None
        return {
            "allowed": rescheduling.allowed,
            "free_reschedule_until": rescheduling.free_reschedule_until or None,
            "max_reschedules": rescheduling.max_reschedules,
            "fee": rescheduling.fee,
        }

    policies = service.policies
    pricing = service.pricing
    summary = {
        "id": service.id,
        "name": service.name,
        "type": service.type,
        "pricing": {
            "amount_minor_units": pricing.amount if pricing else None,
            "price_display": _format_price_display(
                pricing.amount if pricing else None,
                pricing.currency if pricing else None,
            ),
            "amount": pricing.amount if pricing else None,
            "currency": pricing.currency if pricing else None,
            "model": str(pricing.model) if pricing and pricing.model is not None else None,
        },
        "policies": {
            "confirmation_mode": (
                str(policies.confirmation_mode)
                if policies and policies.confirmation_mode is not None
                else None
            ),
            "requires_payment": policies.requires_payment if policies else None,
            "payment_timing": (
                str(policies.payment_timing)
                if policies and policies.payment_timing is not None
                else None
            ),
            # Kept so a cancel that the agent cannot perform itself can still
            # tell the buyer what the merchant's terms are.
            "cancellation": _cancellation_policy_summary(policies),
            "rescheduling": _rescheduling_policy_summary(policies),
        },
        "duration": service.duration.fixed if service.duration else None,
        "availability_hint": availability_hint_summary(service),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    summary.update(location_block_from_service(service))
    return summary
