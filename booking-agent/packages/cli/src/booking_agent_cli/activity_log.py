"""Append-only activity log: the agent audit trail, also read by support reports."""
from __future__ import annotations

import shlex
import sys
from datetime import datetime, timezone

from .session import ActivityEntry, LastError, Session

MAX_ACTIVITY_LOG = 300
# Flags whose values are the user's address or free text written for the business.
PRIVATE_VALUE_FLAGS = frozenset({"--address", "--notes", "--context"})
REDACTED = "<redacted>"


def _redact_private_values(args: list[str]) -> list[str]:
    out: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            out.append(REDACTED)
            redact_next = False
            continue
        name, sep, _ = arg.partition("=")
        if name in PRIVATE_VALUE_FLAGS:
            if sep:
                out.append(f"{name}={REDACTED}")
            else:
                out.append(arg)
                redact_next = True
            continue
        out.append(arg)
    return out


def format_argv(argv: list[str] | None = None) -> str:
    args = argv if argv is not None else sys.argv
    return shlex.join(_redact_private_values(list(args)))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(session: Session, entry: ActivityEntry) -> None:
    session.activity_log.append(entry)
    if len(session.activity_log) > MAX_ACTIVITY_LOG:
        session.activity_log = session.activity_log[-MAX_ACTIVITY_LOG:]


def record_command(session: Session, command: str | None = None) -> None:
    """Log a CLI invocation against the active session."""
    session.last_error = None
    _append(
        session,
        ActivityEntry(
            at=_now_iso(),
            kind="command",
            text=command or format_argv(),
        ),
    )


def record_error(session: Session, code: str, message: str) -> None:
    """Log an agent-facing error and mirror it on ``last_error``."""
    session.last_error = LastError(code=code, message=message)
    _append(
        session,
        ActivityEntry(
            at=_now_iso(),
            kind="error",
            text=message,
            code=code,
        ),
    )


def persist_error_if_session(
    code: str,
    message: str,
    *,
    next_command: str | None = None,
    phase: str | None = None,
    exc: BaseException | None = None,
) -> bool:
    """Best-effort error log when ``emit_error`` runs with an active session.

    Returns True when this error froze a consented support report (spec #307).
    The offer is decided before the error is logged, so a repeat is judged
    against earlier occurrences only.
    """
    from .session import load_session, save_session

    session = load_session()
    if session is None:
        return False
    offered = False
    try:
        from .support_report import maybe_offer

        offered = maybe_offer(session, code, next_command, phase or session.flow_phase, exc)
    except Exception:
        offered = False
    record_error(session, code, message)
    save_session(session, write_support_report=offered)
    return offered
