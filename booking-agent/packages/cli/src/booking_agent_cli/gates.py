"""Shared pieces of the surface-then-confirm gates.

A gate that needs the user's confirmation stores a digest of exactly what it showed.
A later ``--*-confirmed`` / ``--policies-accepted`` flag is honored only while the
digest of the current content still equals it, so a confirmation never covers content
the user did not see.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional


def digest(content: Any) -> str:
    raw = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Time preference (I3)
# ---------------------------------------------------------------------------

_DAY_WORDS = (
    "today", "tonight", "tomorrow", "this morning", "this afternoon", "this evening",
    "this week", "this weekend", "next week", "weekend",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_DAY_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in _DAY_WORDS) + r")\b", re.IGNORECASE)
_CLOCK_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b", re.IGNORECASE)


def named_time(text: Optional[str]) -> Optional[str]:
    """The first day or clock time the text names (for example "tomorrow"), or None."""
    if not text:
        return None
    match = _DAY_RE.search(text) or _CLOCK_RE.search(text)
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# Visit-type mismatch (I5)
# ---------------------------------------------------------------------------

_HOME_VISIT_RE = re.compile(
    r"\b(come (?:to|over|fix|out)|to my (?:home|house|place|apartment|flat|address)|"
    r"at (?:my )?(?:home|house|place|apartment)|in my (?:home|house|apartment)|"
    r"home visit|house call|on[- ]site|visit me|my address)\b",
    re.IGNORECASE,
)


def _row_channel(row: Dict[str, Any]) -> Optional[str]:
    channel = row.get("channel")
    if isinstance(channel, dict):
        value = channel.get("type")
        return value if isinstance(value, str) else None
    return channel if isinstance(channel, str) else None


def request_implies_home_visit(user_request: Optional[str], rows: Iterable[Dict[str, Any]]) -> bool:
    """True when the request asks for a home visit, or the other results are home visits."""
    if user_request and _HOME_VISIT_RE.search(user_request):
        return True
    return any(_row_channel(r) == "at_buyer_location" for r in rows if isinstance(r, dict))


def derived_mismatch_label(
    user_request: Optional[str],
    rows: List[Dict[str, Any]],
    *,
    service_id: Optional[str],
    channel: Optional[str],
) -> Optional[str]:
    """The less-direct label the CLI derives itself when the agent passed none."""
    if channel != "at_business_location":
        return None
    others = [r for r in rows if isinstance(r, dict) and r.get("service_id") != service_id]
    if not request_implies_home_visit(user_request, others):
        return None
    return "at the business location, not a home visit at your address"
