"""Faults the client absorbed, so the user never saw them.

Code that catches an exception and carries on (a business profile that cannot be
read during discovery, a catalog entry that cannot be fetched) calls
``record_silent_fault``. Warnings logged by the allowed modules during a command are
collected the same way. The CLI decides at the end of the command whether these
become automatic fault reports; nothing here does I/O.

Only structural facts are kept: the step, the exception type, a code-authored log
format string, and public identifiers of the business involved. Exception messages
and log arguments are never kept, because they can carry anything.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

#: Steps whose outcome never depends on the user's own accounts or data.
STEPS = frozenset({
    "registry_search",
    "profile_resolve",
    "catalog_enrich",
    "availability_prefetch",
    "platform_profile",
    "cli",
})
#: Loggers whose warnings count as degraded results. Link, payment, identity, buyer
#: details, and Google Calendar code is not here: their faults stay on the consented path.
WARNING_LOGGERS = (
    "booking_agent.backend.live",
    "booking_agent.discovery",
    "booking_agent.headless",
    "booking_agent.ucp_profile",
    "booking_agent.profile_cache",
)
_MAX_RECORDED = 50
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_recorded: List[Dict[str, Any]] = []


def _reason(exc: Optional[BaseException]) -> Optional[str]:
    if exc is None:
        return None
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return f"http_{status}"
    from booking_agent.ucp_profile import UcpProfileValidationError

    if isinstance(exc, UcpProfileValidationError) and exc.args and isinstance(exc.args[0], str):
        # Validation messages are written in this package and name only profile fields.
        slug = _SLUG_RE.sub("_", exc.args[0].lower()).strip("_")
        return slug[:64] or None
    return None


def _subject(profile_url: Optional[str], **ids: Any) -> Dict[str, Any]:
    subject: Dict[str, Any] = {}
    if profile_url:
        try:
            parts = urlsplit(profile_url)
        except ValueError:
            parts = None
        if parts is not None and parts.scheme in ("http", "https") and parts.hostname:
            subject["host"] = parts.hostname
            subject["path"] = parts.path or "/"
    for key, value in ids.items():
        if value is not None:
            subject[key] = value
    return subject


def record_silent_fault(
    step: str,
    exc: Optional[BaseException] = None,
    *,
    profile_url: Optional[str] = None,
    business_id: Optional[str] = None,
    service_id: Optional[str] = None,
) -> None:
    """Note a fault this code absorbed. Never raises."""
    try:
        if len(_recorded) >= _MAX_RECORDED:
            return
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        _recorded.append({
            "fault_class": "absorbed_exception",
            "step": step if step in STEPS else "cli",
            "exception_type": _qualname(exc),
            "message_template": None,
            "reason_code": _reason(exc),
            "subject": _subject(
                profile_url,
                business_id=business_id,
                service_ids=[service_id] if service_id else None,
                http_status=status if isinstance(status, int) else None,
            ),
        })
    except Exception:
        pass


def _qualname(exc: Optional[BaseException]) -> Optional[str]:
    if exc is None:
        return None
    cls = type(exc)
    return f"{cls.__module__}.{cls.__qualname__}"


class _WarningCollector(logging.Handler):
    """Keeps the format string of each warning; the formatted text is never stored."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not record.name.startswith(WARNING_LOGGERS) or len(_recorded) >= _MAX_RECORDED:
                return
            exc = record.exc_info[1] if record.exc_info else None
            _recorded.append({
                "fault_class": "degraded_result",
                "step": "cli",
                "exception_type": _qualname(exc),
                "message_template": record.msg if isinstance(record.msg, str) else None,
                "reason_code": None,
                "subject": {"logger": record.name},
            })
        except Exception:
            pass


_collector: Optional[_WarningCollector] = None


def install_warning_collector() -> None:
    """Collect allowed warnings instead of printing them to stderr (idempotent)."""
    global _collector
    if _collector is not None:
        return
    _collector = _WarningCollector()
    for name in ("booking_agent", "booking_agent_cli"):
        logging.getLogger(name).addHandler(_collector)


def drain() -> List[Dict[str, Any]]:
    """Every fault recorded so far, cleared."""
    out = list(_recorded)
    _recorded.clear()
    return out
