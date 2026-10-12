"""Search area and service address proposals the user must confirm.

A proposal is geocoded by the CLI and shown to the user with the label the
geocoder resolved, so a mistyped or ambiguous address is caught before any
search or booking uses it. Nothing here persists; ``flow search-area`` saves
the proposal on the session.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .geocode import (
    GeocodeResult,
    address_label,
    delivery_address_from_link,
    geocode_structured,
    geocode_text,
)

PURPOSE_SEARCH = "search"
PURPOSE_SERVICE_ADDRESS = "service_address"
SOURCE_LINK = "link_address"
SOURCE_TYPED = "typed"


def link_entry_label(entry: Dict[str, Any]) -> str:
    address = entry.get("address") if isinstance(entry.get("address"), dict) else {}
    return address_label(delivery_address_from_link(address))


def link_address_options(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Saved Link addresses as ``{id, label, is_default}`` for the user to choose from."""
    out = []
    for entry in entries:
        label = link_entry_label(entry)
        if not label or not entry.get("id"):
            continue
        out.append({
            "id": entry["id"],
            "label": label,
            "is_default": entry.get("is_default") is True,
        })
    return out


def _proposal(
    result: GeocodeResult,
    *,
    label: str,
    source: str,
    purpose: str,
    link_address_id: Optional[str] = None,
    typed: Optional[str] = None,
) -> Dict[str, Any]:
    address = dict(result.address) if result.street_level else {}
    if address:
        address["coordinates"] = {"lat": result.lat, "lng": result.lng}
    proposal: Dict[str, Any] = {
        "label": label,
        "matched": result.display_name or label,
        "lat": result.lat,
        "lng": result.lng,
        "source": source,
        "purpose": purpose,
        "street_level": bool(address),
        "address": address or None,
    }
    if link_address_id:
        proposal["link_address_id"] = link_address_id
    if typed:
        proposal["typed"] = typed
    return proposal


def proposal_from_link_entry(entry: Dict[str, Any], *, purpose: str) -> Dict[str, Any]:
    address = entry.get("address") if isinstance(entry.get("address"), dict) else {}
    result = geocode_structured(address)
    return _proposal(
        result,
        label=link_entry_label(entry),
        source=SOURCE_LINK,
        purpose=purpose,
        link_address_id=entry.get("id"),
    )


def proposal_from_text(text: str, *, purpose: str) -> Dict[str, Any]:
    result = geocode_text(text)
    label = address_label(result.address) if result.street_level else result.display_name
    return _proposal(
        result,
        label=label or result.display_name,
        source=SOURCE_TYPED,
        purpose=purpose,
        typed=text.strip(),
    )


def public_view(proposal: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """What the agent shows the user: labels and precision, never raw coordinates."""
    if not proposal:
        return None
    view = {
        "label": proposal.get("label"),
        "matched": proposal.get("matched"),
        "source": proposal.get("source"),
        "street_level": bool(proposal.get("street_level")),
    }
    if proposal.get("typed"):
        view["typed"] = proposal["typed"]
    return view
