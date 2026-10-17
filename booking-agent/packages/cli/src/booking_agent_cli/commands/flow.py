"""``booking-agent flow ...`` — stateful USP booking flow with hard gates.

Every subcommand emits one agent-facing JSON object (see ``output.AgentOutput``)
and persists ``.usp-session.json``. Gates that fail exit code 1 with an
agent-facing error payload — the agent is expected to read ``next_command``
and either ask the user for input or run the suggested command.
"""
from __future__ import annotations

import asyncio
import contextlib
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from booking_agent import settings
from booking_agent.calendar.scope import (
    CALENDAR_MODE_EVENTS,
    CALENDAR_MODE_FREEBUSY,
    CALENDAR_MODE_NONE,
    connection_capabilities,
    inspect_access_token,
    scope_satisfies_mode,
)
from booking_agent.platform_profile import platform_profile_url
from booking_agent.pop import PopKeyStore
from booking_agent.http_deadlines import coerce_timeout
from booking_agent.rest_client import (
    BookingServerError,
    PopAuthorizationError,
    RegistrySearchRejectedError,
    RevisionMismatch,
)
from booking_agent.ucp_checkout import CompleteOutcome, UcpCheckoutHttpError, UcpCompleteOutcomeError
from booking_agent.ucp_profile import UcpProfileValidationError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
import typer

from ..checkout_prereq import (
    PrereqFailure,
    run_prereq_check_ucp,
    select_checkout_mode,
    supported_checkout_modes_from_profile,
)
from ..link_cli import (
    default_link_cli_bin,
    SPEND_REQUIRES_ACTION,
    SharedPaymentTokenUnavailable,
    SpendActionRequired,
    SpendNextAction,
    create_spend_request,
    get_auth_status,
    initiate_auth_login,
    poll_auth_until_authenticated,
    retrieve_spend_request,
    wait_for_spend_approval,
)
from ..payment_helpers import (
    PROFILE_UNRESOLVABLE_CODE,
    apply_booking_to_session,
    approval_poll_deadline,
    cancel_ucp_checkout_async,
    create_booking_sync,
    payment_path_summary,
    resolve_spend_request_fields_sync,
    resolve_spend_approval_url,
)
from ..calendar_connection import (
    CALENDAR_ROUTES,
    ROUTE_CONNECTOR,
    ROUTE_LOCAL_OAUTH,
    cache_host_token,
    calendar_connection_status,
    host_token,
    local_oauth_configured,
    refresh_host_token,
    remember_route,
    remembered_route,
    scope_request,
    valid_token_env_name,
)
from ..calendar_event import (
    create_calendar_event,
    delete_calendar_event,
    load_cached_token,
    update_calendar_event,
)
from ..calendar_preference import (
    complete_calendar_gate_with_preference,
    get_calendar_preference,
    remembered_preference_applicable,
    skip_calendar_gate_session_only,
    stale_remembered_calendar_mode,
    try_apply_remembered_calendar_gate,
)
from ..booking_poll import (
    INACTIVE_BOOKING_STATUSES,
    BookingPollTimeout,
    apply_booking_status,
    booking_status_value,
    fetch_booking,
    wait_for_booking_terminal,
)
from ..engine import (
    GateError,
    recommend_next,
    require_business,
    require_buyer_from_link_cli,
    require_calendar_gate,
    require_checkout_prereq,
    require_link_auth_gate,
    require_phase,
    require_slot_selected,
    transition,
)
from ..output import (
    AWAITING_LINK_AUTH_APPROVAL,
    AWAITING_LINK_SPEND_APPROVAL,
    LINK_AUTH_POLL_BACKGROUND_INSTRUCTION,
    PAY_POLL_BACKGROUND_INSTRUCTION,
    AgentOutput,
    emit,
    emit_error,
    spend_approval_surface,
    write_spend_approval_notice,
)
from ..runtime import (
    ProfileUrlError,
    require_fetchable_profile_url,
    resolve_backend,
    resolve_calendar_service,
)
from .. import reschedule as reschedule_rules
from .. import timezone_context as tz_context
from ..activity_log import record_command
from ..identity import logout_identity
from ..session import (
    BUYER_SOURCE_LINKCLI,
    SCHEMA_VERSION,
    TERMINAL_PHASES,
    AvailabilityWindow,
    BookingRef,
    Business,
    Buyer,
    LastError,
    PaymentRef,
    Session,
    SlotRef,
    add_session,
    clear_credential,
    credential_for_session_resource,
    delete_session,
    endpoint_still_in_use,
    forget_session,
    get_active_session,
    is_actionable,
    load_session,
    load_store,
    must_load,
    save_session,
    save_store,
    session_slot_end,
    set_active_session,
)

app = typer.Typer(help="Stateful USP booking flow with hard human gates.")


@app.callback()
def _flow_setup(ctx: typer.Context) -> None:
    """Load config for every command process.

    Each ``booking-agent flow ...`` invocation is a separate process, so config is
    re-read on every call: the process environment, then the user and folder ``.env``
    files, then the shipped defaults.
    """
    from booking_agent.http_trace import clear_last_http_failure

    from .. import config_loader

    clear_last_http_failure()
    sub = ctx.invoked_subcommand
    if sub not in (None, "init", "sessions"):
        session = load_session()
        if session is not None:
            record_command(session)
            save_session(session)
    config_loader.load()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FORBIDDEN = [
    "POST /bookings with raw curl",
    "POST /availability/query with raw curl",
    "Hand-entered or placeholder buyer details",
    "Selecting a slot without user confirmation",
]


def _agent_out(
    session: Session,
    *,
    awaiting_user: Optional[str] = None,
    user_message: Optional[str] = None,
    prompt: Optional[str] = None,
    next_command: Optional[str] = None,
    data: Optional[dict] = None,
) -> AgentOutput:
    from ..flow_timings import timings_data

    merged = dict(data or {})
    merged.update(timings_data(session))
    return AgentOutput(
        phase=session.flow_phase,
        awaiting_user=awaiting_user,
        user_message=user_message,
        prompt=prompt,
        next_command=next_command or recommend_next(session),
        forbidden=list(FORBIDDEN),
        data=merged,
    )


def _handle_gate(session: Session, err: GateError) -> "typer.Exit":
    emit_error(
        session.flow_phase,
        code=err.code,
        message=err.message,
        next_command=err.next_command,
        forbidden=list(FORBIDDEN),
    )
    return typer.Exit(code=1)


def _emit_flow_http_error(
    phase: str,
    exc: BaseException,
    *,
    default_code: str,
) -> "typer.Exit":
    from booking_agent.http_retry import is_rate_limited, retry_after_from_exception

    if is_rate_limited(exc):
        retry_after = retry_after_from_exception(exc)
        extra: dict = {}
        if retry_after is not None:
            extra["retry_after_seconds"] = retry_after
        emit_error(
            phase,
            code="rate_limited",
            message=str(exc),
            data=extra or None,
        )
    else:
        emit_error(phase, code=default_code, message=str(exc))
    return typer.Exit(code=1)


AGENT_PROFILE_UNRESOLVABLE_USER_MESSAGE = (
    "The business couldn't verify the booking assistant just now, so nothing was "
    "booked. It's safe to try again."
)


def _emit_booking_create_error(
    phase: str,
    exc: BaseException,
    *,
    default_code: str,
    retry_command: str,
) -> "typer.Exit":
    """Report a failed booking creation from ``flow book`` or ``flow pay rebook``."""
    from booking_agent.rest_client import BookingRequestError

    if not isinstance(exc, BookingRequestError):
        emit_error(phase, code=default_code, message=str(exc))
        return typer.Exit(code=1)
    business_error = {"http_status": exc.http_status, "business_code": exc.business_code}
    if exc.business_code == PROFILE_UNRESOLVABLE_CODE:
        emit_error(
            phase,
            code="agent_profile_unresolvable",
            message=(
                f"The business could not fetch the agent platform profile "
                f"(HTTP {exc.http_status}, code {exc.business_code}), so nothing was booked. "
                f"Run `{retry_command}` again; it resends the same Idempotency-Key, so the "
                "retry cannot create a second booking."
            ),
            next_command=retry_command,
            user_message=AGENT_PROFILE_UNRESOLVABLE_USER_MESSAGE,
            data={"business_error": business_error},
        )
    else:
        emit_error(
            phase,
            code=default_code,
            message=str(exc),
            data={"business_error": business_error},
        )
    return typer.Exit(code=1)


# ---------------------------------------------------------------------------
# init / status
# ---------------------------------------------------------------------------


@app.command("init")
def init(
    user_request: str = typer.Option(..., "--user-request", help="Natural-language request that started this flow"),
    force: bool = typer.Option(False, help="Overwrite an active incomplete session"),
    allow_duplicate: bool = typer.Option(
        False, "--allow-duplicate",
        help="The user confirmed they want another booking despite a matching upcoming one.",
    ),
    skill_version: Optional[str] = typer.Option(
        None, "--skill-version", help="The version in the loaded skill's frontmatter.",
    ),
    capability: Optional[List[str]] = typer.Option(
        None, "--capability", help="A capability the user's request needs; repeatable.",
    ),
) -> None:
    """Initialize a new booking session."""
    store = load_store()
    active = get_active_session(store)
    if active is not None and active.flow_phase not in TERMINAL_PHASES and not force:
        emit_error(
            "INIT",
            code="session_active",
            message=(
                f"Active session '{active.id}' is in phase '{active.flow_phase}' "
                "(not yet completed). Pass --force to start a new session anyway, "
                f"or resume with `booking-agent flow sessions resume --session-id {active.id}`."
            ),
            next_command=f"booking-agent flow sessions resume --session-id {active.id}",
        )
        raise typer.Exit(code=1)

    gate = _update_gate(user_request, skill_version, capability or [], allow_duplicate)
    if gate is not None:
        emit(gate)
        return

    duplicates = [] if allow_duplicate else _upcoming_duplicate_bookings(store, user_request)
    if duplicates:
        rerun = (
            f"booking-agent flow init --user-request {shlex.quote(user_request)} "
            "--allow-duplicate"
        )
        emit(AgentOutput(
            phase="INIT",
            awaiting_user="duplicate_booking_confirmation",
            prompt=(
                "The user already has an upcoming booking that looks like this request "
                "(data.upcoming_matches). Before starting a new booking, tell them about "
                "it in one sentence (service, business, time) and ask whether they want an "
                "additional booking. STOP for their answer. Run next_command only if they "
                "want another one. Never cancel, reschedule, or resume the existing "
                "booking unless they ask."
            ),
            next_command=rerun,
            data={"upcoming_matches": duplicates},
        ))
        return

    try:
        init_data: dict = {"agent_profile_url": require_fetchable_profile_url()}
    except ProfileUrlError as e:
        emit_error("INIT", code="platform_profile_url_invalid", message=str(e))
        raise typer.Exit(code=1)

    # Skip prereq check for all sessions after the very first (sessions_created tracks
    # lifetime count so it never resets to 0 even after `flow reset`).
    is_first_session = store.sessions_created == 0
    initial_phase = "INIT" if is_first_session else "PREREQ_OK"

    session = Session(
        user_request=user_request,
        schema_version=SCHEMA_VERSION,
        flow_phase=initial_phase,
    )
    try:
        if get_auth_status().authenticated:
            session.link_auth_gate_completed = True
    except Exception:
        pass
    add_session(store, session)
    save_store(store)
    from ..session import bind_session_observability

    bind_session_observability(session)
    record_command(session)
    save_session(session)

    if not session.link_auth_gate_completed:
        prompt = (
            "Session initialized. The user is not signed in to Link: run next_command now, "
            "before any question or search, without asking the user first."
        )
        emit(_agent_out(
            session, prompt=prompt, next_command=LINK_AUTH_CONNECT, data=init_data,
        ))
        return
    if is_first_session:
        prompt = "Session initialized. Run `booking-agent flow prereq check` next."
    else:
        prompt = (
            "Session initialized (prerequisite check skipped — already verified in a "
            "prior session). Ready to discover services."
        )

    emit(_agent_out(session, prompt=prompt, data=init_data))


def _update_gate(
    user_request: str, skill_version: Optional[str], capabilities: List[str], allow_duplicate: bool,
) -> Optional[AgentOutput]:
    """Ask about an update before a session exists, only for a mismatch or a ready release."""
    from . import update as update_cmd
    from .. import updates

    try:
        decision = update_cmd.current_decision(
            reported_skill=skill_version,
            capabilities=capabilities,
            after_booking=False,
            check_local=True,
        )
    except Exception:
        return None
    if decision.action not in updates.PROMPT_ACTIONS:
        return None
    rerun = f"booking-agent flow init --user-request {shlex.quote(user_request)}"
    if skill_version:
        rerun += f" --skill-version {shlex.quote(skill_version)}"
    for c in capabilities:
        rerun += f" --capability {shlex.quote(c)}"
    if allow_duplicate:
        rerun += " --allow-duplicate"
    return update_cmd.consent_output(decision, phase="INIT", continue_command=rerun)


_REQUEST_STOPWORDS = frozenset(
    "a an and at book booking but can come do don for from get have help home i in is it "
    "its know me my need next no not of on or please some someone that the this to want "
    "with".split()
)


def _request_terms(text: Optional[str]) -> set:
    words = "".join(c.lower() if c.isalnum() else " " for c in text or "").split()
    return {w for w in words if len(w) > 2 and w not in _REQUEST_STOPWORDS}


def _upcoming_duplicate_bookings(store: Any, user_request: str) -> List[Dict[str, Any]]:
    """Upcoming bookings whose original request covers most of this one's key terms."""
    wanted = _request_terms(user_request)
    if not wanted:
        return []
    matches = []
    for s in reversed(store.sessions):
        if not s.booking.id or not is_actionable(s):
            continue
        prior = _request_terms(s.user_request) | _request_terms(
            (s.live_service or {}).get("name")
        )
        if len(wanted & prior) / len(wanted) < 0.6:
            continue
        selected = next((sl for sl in s.slots_presented if sl.slot_id == s.slot_id), None)
        matches.append({
            "session_id": s.id,
            "booking_id": s.booking.id,
            "booking_status": s.booking.status,
            "business_name": s.business.name,
            "service_name": (s.live_service or {}).get("name"),
            "slot_start": selected.start if selected else None,
            "slot_start_display": (
                tz_context.format_instant(selected.start, s.business.timezone)
                if selected else None
            ),
            "user_request": s.user_request,
        })
    return matches


@app.command("status")
def status() -> None:
    session = load_session()
    if session is None:
        emit_error(
            "INIT",
            code="no_session",
            message="No `.usp-session.json` here. Run `booking-agent flow init` first.",
            next_command="booking-agent flow init",
        )
        raise typer.Exit(code=1)
    from ..support_report import status_gate

    gate = status_gate(session)
    if gate is not None:
        emit(gate)
        return
    prompt = None
    next_cmd = None
    awaiting = None
    extra_data: dict = {}
    if session.payment.captured_unconfirmed:
        next_cmd = "booking-agent flow book poll --wait"
        prompt = (
            "The user's payment was captured but the business has not confirmed the "
            "booking. Never retry the payment or create a new spend request. Run "
            "next_command to check the booking again."
        )
    elif session.flow_phase == "PAYMENT_PENDING" and session.payment.spend_request_id:
        sr_id = session.payment.spend_request_id
        approval_url = resolve_spend_approval_url(sr_id)
        next_cmd, prompt = _payment_pending_next(session, sr_id)
        if approval_url:
            extra_data["approval_url"] = approval_url
    elif session.flow_phase == "CONFIRMED" and session.reschedule_slot_id:
        awaiting = "reschedule_confirm"
        prompt = _reschedule_confirm_prompt(session)
    elif session.flow_phase == "CONFIRMED" and session.reschedule_original_slot_id:
        prompt = (
            "A move of this booking is in progress. Continue with next_command, or run "
            "`booking-agent flow reschedule abort` if the user no longer wants to move it."
        )
    emit(_agent_out(
        session,
        awaiting_user=awaiting,
        prompt=prompt,
        next_command=next_cmd,
        data={"session": _session_data(session), **extra_data},
    ))


@app.command("evidence")
def flow_evidence(
    booking_id: Optional[str] = typer.Option(
        None,
        "--booking-id",
        help="Booking id to look up in session history (defaults to active session)",
    ),
) -> None:
    """Print authorization join keys for a booking (no credentials or card data)."""
    session: Optional[Session]
    if booking_id and booking_id.strip():
        store = load_store()
        session = next(
            (s for s in store.sessions if s.booking.id == booking_id.strip()),
            None,
        )
        if session is None:
            emit_error(
                "INIT",
                code="booking_not_in_sessions",
                message=f"No session found with booking id {booking_id.strip()!r}.",
            )
            raise typer.Exit(code=1)
    else:
        session = load_session()
        if session is None:
            emit_error(
                "INIT",
                code="no_session",
                message="No `.usp-session.json` here. Run `booking-agent flow init` first.",
                next_command="booking-agent flow init",
            )
            raise typer.Exit(code=1)

    emit(_agent_out(
        session,
        prompt=(
            "Authorization join keys for this booking. Use spend_request_id with "
            "link-cli spend-request retrieve when you need Link approval evidence."
        ),
        data={
            "correlation_id": session.correlation_id,
            "spend_request_id": session.payment.spend_request_id,
            "declined_spend_request_id": session.payment.declined_spend_request_id,
            "ucp_checkout_session_id": session.ucp_checkout_session_id,
            "booking_id": session.booking.id,
            "order_id": session.payment.order_id,
        },
    ))


@app.command("logout")
def flow_logout() -> None:
    """Log out of Link and Google Calendar; keep upcoming bookings cancellable."""
    result = logout_identity()
    emit(AgentOutput(
        phase="STORE",
        data=result,
        prompt=(
            "Signed you out of Link and disconnected Google Calendar on this device. "
            "Your upcoming bookings are still saved so you can cancel them."
        ),
    ))


@app.command("reset")
def reset() -> None:
    """Remove the active session from the history store."""
    removed = delete_session()
    if not removed:
        typer.echo("No active session to remove.")
        raise typer.Exit(code=1)
    typer.echo("Active session removed.")


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

sessions_app = typer.Typer(help="Session history management.")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list() -> None:
    """List all sessions in the history store, newest first."""
    store = load_store()
    sessions_data = []
    for s in reversed(store.sessions):
        slot_end = session_slot_end(s)
        selected = next((sl for sl in s.slots_presented if sl.slot_id == s.slot_id), None)
        sessions_data.append({
            "id": s.id,
            "created_at": s.created_at,
            "user_request": s.user_request,
            "flow_phase": s.flow_phase,
            "business_name": s.business.name,
            "booking_id": s.booking.id,
            "booking_status": s.booking.status,
            "slot_start": selected.start if selected else None,
            "slot_end": selected.end if selected else None,
            "actionable": is_actionable(s),
            "active": s.id == store.active_session_id,
        })
    emit(AgentOutput(
        phase="STORE",
        data={"sessions": sessions_data, "total": len(store.sessions)},
    ))


@sessions_app.command("forget")
def sessions_forget(
    session_id: Optional[str] = typer.Option(
        None, "--session-id", help="Session to remove (default: active session).",
    ),
) -> None:
    """Remove a booking session from local history (does not log out Link or Calendar)."""
    store = load_store()
    target_id = session_id
    if target_id is None:
        active = get_active_session(store)
        if active is None:
            emit_error(
                "STORE",
                code="no_session",
                message="No active session to forget.",
            )
            raise typer.Exit(code=1)
        target_id = active.id
    if not forget_session(store, target_id):
        emit_error(
            "STORE",
            code="session_not_found",
            message=f"Session '{target_id}' not found in store.",
        )
        raise typer.Exit(code=1)
    save_store(store)
    emit(AgentOutput(
        phase="STORE",
        data={"forgotten_session_id": target_id},
        prompt="Removed that booking from local history.",
    ))


@sessions_app.command("resume")
def sessions_resume(
    session_id: str = typer.Option(..., "--session-id", help="ID of session to resume"),
) -> None:
    """Set a session as active so subsequent flow commands operate on it."""
    store = load_store()
    if not set_active_session(store, session_id):
        emit_error(
            "STORE",
            code="session_not_found",
            message=f"Session '{session_id}' not found in store.",
        )
        raise typer.Exit(code=1)
    save_store(store)
    session = get_active_session(store)
    from ..session import bind_session_observability

    bind_session_observability(session)
    emit(_agent_out(
        session,
        prompt=f"Resumed session. Current phase: {session.flow_phase}.",
        data={"session_id": session_id, "flow_phase": session.flow_phase},
    ))


CANCEL_OPEN_PREVIEW_PROMPT = (
    "Read-only preview: nothing was cancelled or removed. Show the user each upcoming "
    "appointment (business, time, and their original request) and each unfinished request "
    "in everyday words. Unless the user already explicitly said to cancel all of them, ask "
    "which ones to include and STOP. Never assume rows from testing or from other requests "
    "belong to this request. Then run `booking-agent flow sessions cancel-open --user-confirmed` "
    "with one `--session-id` per chosen row (or `--all` only when the user said all), and "
    "add `--forget-unfinished` only if the user agreed to drop unfinished requests. Rows "
    "under `held` have a payment in progress: do not offer to drop them."
)


def _cancel_open_kind(session: Session) -> tuple[Optional[str], Optional[str]]:
    """``(kind, skip_reason)`` where kind is booking, unfinished, held, or skipped."""
    if session.booking.id:
        if session.booking.status in INACTIVE_BOOKING_STATUSES:
            return "skipped", "booking_inactive"
        if is_actionable(session):
            return "booking", None
        return "skipped", "booking_not_actionable"
    if session.flow_phase in TERMINAL_PHASES:
        return "skipped", "session_finished"
    payment = session.payment
    if payment.captured_unconfirmed or payment.spend_request_id or payment.status == "paid":
        return "held", "payment_in_progress"
    return "unfinished", None


def _cancel_open_row(session: Session, active_id: Optional[str]) -> Dict[str, Any]:
    selected = next((sl for sl in session.slots_presented if sl.slot_id == session.slot_id), None)
    return {
        "session_id": session.id,
        "created_at": session.created_at,
        "user_request": session.user_request,
        "flow_phase": session.flow_phase,
        "business_name": session.business.name,
        "booking_id": session.booking.id,
        "booking_status": session.booking.status,
        "slot_start": selected.start if selected else None,
        "slot_end": selected.end if selected else None,
        "active": session.id == active_id,
    }


@contextlib.contextmanager
def _session_environment(session: Session):
    """Refuse to act on *session* without a fetchable platform profile URL."""
    require_fetchable_profile_url()
    yield


def _bulk_cancel_one(session: Session, reason: Optional[str]) -> Dict[str, Any]:
    from ..session import bind_session_observability

    bid = session.booking.id
    bind_session_observability(session)
    try:
        with _session_environment(session):
            status_val, settlement = _cancel_and_record(session, bid, reason)
    except _CancelNeedsHuman as e:
        return {
            "outcome": "needs_human",
            "error": {"code": e.code, "message": e.message},
            "booking_url": e.booking_url,
            **e.handoff,
        }
    except _CancelFailed as e:
        return {"outcome": "failed", "error": {"code": "cancel_failed", "message": e.message}, **e.data}
    except ProfileUrlError as e:
        return {"outcome": "failed", "error": {"code": "platform_profile_url_invalid", "message": str(e)}}
    except Exception as e:
        return {
            "outcome": "failed",
            "error": {"code": "cancel_failed", "message": f"Cancellation failed ({type(e).__name__})."},
        }
    finally:
        bind_session_observability(None)
    row: Dict[str, Any] = {
        "outcome": "canceled" if status_val == "canceled" else "inactive",
        "status": status_val,
        "cancellation": settlement.get("cancellation"),
        "refund_follow_up": settlement.get("refund_follow_up"),
    }
    if status_val == "canceled":
        calendar = _remove_calendar_event_after_cancel(session)
        message = _cancel_user_message(session, settlement)
        if calendar and calendar.get("user_note"):
            message += "\n" + calendar["user_note"]
        row["calendar"] = calendar
        row["user_message"] = message
    return row


@sessions_app.command("cancel-open")
def sessions_cancel_open(
    session_ids: Optional[List[str]] = typer.Option(
        None, "--session-id", help="Session to include; repeat for each one the user chose.",
    ),
    all_sessions: bool = typer.Option(
        False, "--all", help="Include every upcoming booking (only when the user said all).",
    ),
    forget_unfinished: bool = typer.Option(
        False,
        "--forget-unfinished",
        help="Also remove selected unfinished requests that never booked.",
    ),
    user_confirmed: bool = typer.Option(
        False, "--user-confirmed", help="The user approved exactly this selection.",
    ),
    reason: Optional[str] = typer.Option(None, "--reason", help="Cancellation reason."),
) -> None:
    """Preview, then cancel upcoming bookings and drop unfinished requests the user chose."""
    store = load_store()
    original_active = store.active_session_id
    if session_ids and all_sessions:
        emit_error(
            "STORE",
            code="cancel_open_selection_conflict",
            message="Pass either --session-id (repeatable) or --all, not both.",
        )
        raise typer.Exit(code=1)
    known = {s.id for s in store.sessions}
    unknown = [sid for sid in session_ids or [] if sid not in known]
    if unknown:
        emit_error(
            "STORE",
            code="session_not_found",
            message=f"Session(s) not found in store: {', '.join(unknown)}.",
            data={"unknown_session_ids": unknown},
        )
        raise typer.Exit(code=1)

    explicit = set(session_ids or [])
    ordered = list(reversed(store.sessions))
    if explicit:
        ordered = [s for s in ordered if s.id in explicit]
    classified = [(s, *_cancel_open_kind(s)) for s in ordered]

    if not user_confirmed:
        groups: Dict[str, List[Dict[str, Any]]] = {
            "bookings": [], "unfinished": [], "held": [], "skipped": [],
        }
        for s, kind, why in classified:
            if kind == "skipped" and not explicit:
                continue
            row = _cancel_open_row(s, original_active)
            if why:
                row["skip_reason"] = why
            groups["bookings" if kind == "booking" else kind].append(row)
        emit(AgentOutput(
            phase="STORE",
            prompt=CANCEL_OPEN_PREVIEW_PROMPT,
            data={"mode": "preview", **groups},
        ))
        return

    if not explicit and not all_sessions:
        emit_error(
            "STORE",
            code="cancel_open_selection_required",
            message=(
                "Pass one --session-id per row the user chose, or --all only when the "
                "user explicitly asked to cancel all of them."
            ),
            next_command="booking-agent flow sessions cancel-open",
        )
        raise typer.Exit(code=1)

    results: List[Dict[str, Any]] = []
    to_forget: List[str] = []
    for s, kind, why in classified:
        row = _cancel_open_row(s, original_active)
        if kind == "booking":
            fresh = load_store()
            set_active_session(fresh, s.id)
            save_store(fresh)
            row.update(_bulk_cancel_one(s, reason))
        elif kind == "unfinished" and forget_unfinished:
            to_forget.append(s.id)
            row["outcome"] = "forgotten"
        elif all_sessions and kind in ("unfinished", "skipped"):
            continue
        else:
            row["outcome"] = "skipped"
            row["skip_reason"] = why or "forget_unfinished_not_authorized"
        results.append(row)

    store = load_store()
    for sid in to_forget:
        forget_session(store, sid)
    if original_active:
        set_active_session(store, original_active)
    save_store(store)

    summary: Dict[str, int] = {}
    for row in results:
        summary[row["outcome"]] = summary.get(row["outcome"], 0) + 1
    emit(AgentOutput(
        phase="STORE",
        prompt=(
            "Tell the user plainly which appointments are cancelled and which requests were "
            "removed. For each `canceled` row, post its `user_message` as is: it says whether "
            "the money was refunded and, when it was not, the numbered steps to ask the "
            "business for the refund (business link, amount, booking and order references). "
            "Do not shorten those steps. For `needs_human` rows, show the raw `booking_url` "
            "(or the first `cancel_urls` entry) and say they finish that cancellation on the "
            "business's page. For `failed` rows, say it did not go through and share the same link when "
            "present. Do not retry automatically."
        ),
        data={
            "mode": "applied",
            "results": results,
            "summary": summary,
            "active_session_id": store.active_session_id,
        },
    ))


def _payment_pending_next(
    session: Session,
    spend_request_id: str,
) -> tuple[str, str]:
    """next_command and prompt when resuming PAYMENT_PENDING with a spend request."""
    if (
        session.payment.spend_approval_surfaced
        and not session.payment.spend_approval_user_surfaced
    ):
        return (
            "booking-agent flow pay poll",
            (
                "Post user_message to the user as the raw URL (never markdown [label](url)) "
                f"in this reply. {PAY_POLL_BACKGROUND_INSTRUCTION} The poll completes "
                "checkout, validates skill compliance, and creates a calendar event when needed."
            ),
        )
    if session.checkout_mode == "ucp_native":
        cmd = f"booking-agent flow pay ucp-complete --spend-request-id {spend_request_id}"
    else:
        cmd = f"booking-agent flow pay ucp-complete --spend-request-id {spend_request_id}"
    if session.payment.spend_approval_surfaced:
        prompt = (
            "Payment pending. Run next_command in the foreground (do NOT use run_in_background) "
            "to poll Link approval and finish the booking (approval URL was already shown). "
            "Do not ask the user to confirm in chat."
        )
    else:
        prompt = (
            "Payment pending. Run `booking-agent flow pay request` to create a spend request and "
            "surface the approval URL once, or run next_command if a spend request already exists."
        )
    return cmd, prompt


def _refuse_charge_after_capture(session: Session) -> None:
    """No second charge while a captured payment's booking is unresolved."""
    if not session.payment.captured_unconfirmed:
        return
    emit_error(
        session.flow_phase,
        code="payment_already_captured",
        message=(
            "The user's payment for this booking was already captured and the business "
            "has not confirmed the booking. Do not charge again. Run next_command to "
            "check the booking."
        ),
        next_command="booking-agent flow book poll --wait",
        data={"booking_id": session.booking.id},
    )
    raise typer.Exit(code=1)


def _require_approval_gate_before_charge(session: Session) -> None:
    """Block every completion path until pay request surfaced the trusted UI once."""
    sr_id = session.payment.spend_request_id
    if not sr_id:
        emit_error(
            session.flow_phase,
            code="no_spend_request",
            message="No spend request on session. Run `booking-agent flow pay request` first.",
            next_command="booking-agent flow pay request --payment-method-id <id>",
        )
        raise typer.Exit(code=1)
    if not session.payment.spend_approval_surfaced:
        emit_error(
            session.flow_phase,
            code="approval_not_created",
            message=(
                "Spend approval URL was not surfaced via `pay request`. "
                "Run `pay request` so the approval link is shown to the user."
            ),
            next_command="booking-agent flow pay request --payment-method-id <id>",
            data={"spend_request_id": sr_id},
        )
        raise typer.Exit(code=1)
    if not session.payment.spend_approval_user_surfaced:
        approval_url = resolve_spend_approval_url(sr_id)
        emit_error(
            session.flow_phase,
            code="approval_not_surfaced_to_user",
            message=(
                "Show user_message (the Link approval URL) to the user as the raw URL before "
                f"completing checkout. {PAY_POLL_BACKGROUND_INSTRUCTION}"
            ),
            user_message=approval_url,
            next_command="booking-agent flow pay poll",
            data={"spend_request_id": sr_id},
        )
        raise typer.Exit(code=1)


def _require_spend_approval_surfaced_to_user(session: Session) -> None:
    """Block polling until the agent has acked surfacing the approval URL to the user."""
    _require_approval_gate_before_charge(session)


def _spend_action_data(
    spend_request_id: str, action: Optional[SpendNextAction],
) -> dict:
    return {
        "spend_request_id": spend_request_id,
        "next_action": (
            {
                "type": action.type,
                "resolution": action.resolution,
                "display_message": action.display_message,
                "action_url": action.action_url,
            }
            if action
            else None
        ),
    }


def _spend_action_user_message(action: Optional[SpendNextAction]) -> Optional[str]:
    if action is None:
        return None
    parts = [p for p in (action.display_message, action.action_url) if p]
    return "\n".join(parts) or None


def _emit_spend_action_awaiting(
    session: Session,
    spend_request_id: str,
    action: SpendNextAction,
    *,
    data: Optional[dict] = None,
) -> None:
    """Show an ``auto_resume`` action (issuance 3DS) once; ``pay poll`` then waits on the same request."""
    session.payment.spend_action_surfaced = action.key(spend_request_id)
    session.payment.spend_approval_user_surfaced = False
    save_session(session)
    emit(_agent_out(
        session,
        awaiting_user=AWAITING_LINK_SPEND_APPROVAL,
        user_message=_spend_action_user_message(action),
        prompt=(
            "STOP. The user's bank needs a quick verification before this payment can go "
            "through. This is not a payment failure. Post user_message to the user (the "
            f"bank's message, then the raw URL; never markdown [label](url)) in this reply. "
            f"{PAY_POLL_BACKGROUND_INSTRUCTION} The poll keeps waiting on the same payment "
            "request; do not create a new one."
        ),
        next_command="booking-agent flow pay poll",
        data={**(data or {}), **_spend_action_data(spend_request_id, action)},
    ))


def _require_new_spend_request(
    session: Session,
    spend_request_id: str,
    action: Optional[SpendNextAction],
) -> "typer.Exit":
    """Retire a spend request Link will not resume (e.g. ``three_d_secure_retry``)."""
    session.payment.spend_approval_surfaced = False
    session.payment.spend_approval_user_surfaced = False
    session.payment.spend_action_surfaced = None
    save_session(session)
    payment_method = session.payment.payment_method_id or "<id>"
    if action is not None and action.type == "three_d_secure_retry":
        message = (
            "The bank verification has to be retried on a new payment request. Show "
            "user_message to the user, then create a new spend request with their "
            "approval. The current spend request cannot be completed."
        )
    else:
        message = (
            "Link needs the user to take an action before paying, and this spend "
            "request cannot be resumed. Show user_message to the user, then create a "
            "new spend request with their approval."
        )
    emit_error(
        session.flow_phase,
        code="spend_request_replacement_required",
        message=message,
        user_message=_spend_action_user_message(action),
        next_command=(
            f"booking-agent flow pay request --payment-method-id {payment_method}"
        ),
        data=_spend_action_data(spend_request_id, action),
    )
    return typer.Exit(code=1)


def _poll_spend_approval_or_exit(
    session: Session,
    spend_request_id: str,
    *,
    approval_timeout: int,
    poll_interval: float,
    next_command: str,
) -> None:
    """Block until Link grants the spend request; exit on timeout, denial, or a required action."""
    from booking_agent.step_timing import timed_human_gate

    try:
        deadline = approval_poll_deadline(session, approval_timeout)
        with timed_human_gate("link_spend_approval"):
            sr = wait_for_spend_approval(
                spend_request_id,
                deadline_ts=deadline,
                poll_interval=poll_interval,
                surfaced_action_key=session.payment.spend_action_surfaced,
            )
        session.payment.approved_at = sr.approved_at or datetime.now(timezone.utc).isoformat()
        save_session(session)
        from ..flow_timings import apply_pending_timings

        apply_pending_timings(session)
    except SpendActionRequired as required:
        if required.action.auto_resume:
            _emit_spend_action_awaiting(session, spend_request_id, required.action)
            raise typer.Exit(code=0)
        raise _require_new_spend_request(session, spend_request_id, required.action)
    except TimeoutError:
        emit_error(
            session.flow_phase,
            code="approval_timeout",
            message=(
                "Link approval not received in time. Re-run next_command to poll again."
            ),
            next_command=next_command,
            data={"spend_request_id": spend_request_id},
        )
        raise typer.Exit(code=1)
    except RuntimeError as e:
        emit_error(
            session.flow_phase,
            code="spend_request_not_approved",
            message=str(e),
            next_command=next_command,
            data={"spend_request_id": spend_request_id},
        )
        raise typer.Exit(code=1)


def _session_data(s: Session) -> dict:
    return {
        "flow_phase": s.flow_phase,
        "checkout_mode": s.checkout_mode,
        "supported_checkout_modes": list(s.supported_checkout_modes),
        "ucp_checkout_session_id": s.ucp_checkout_session_id,
        "ucp_spt_network_id": s.ucp_spt_network_id,
        "business": {
            "profile_url": s.business.profile_url,
            "rest_endpoint": s.business.rest_endpoint,
            "name": s.business.name,
            "timezone": s.business.timezone,
        },
        "calendar_gate_completed": s.calendar_gate_completed,
        "link_auth_gate_completed": s.link_auth_gate_completed,
        "calendar_state": s.calendar_state,
        "availability_window": {
            "from": s.availability_window.from_,
            "to": s.availability_window.to,
        },
        "slots_presented": [
            {"slot_id": sl.slot_id, "start": sl.start, "end": sl.end}
            for sl in s.slots_presented
        ],
        "slot_id": s.slot_id,
        "slot_user_confirmed": s.slot_user_confirmed,
        "reschedule": {
            "in_progress": s.reschedule_original_slot_id is not None,
            "original_slot_id": s.reschedule_original_slot_id,
            "slot_count": len(s.reschedule_slots),
            "selected_slot_id": s.reschedule_slot_id,
        },
        "service_id": s.service_id,
        "live_service": s.live_service,
        "ucp_checkout_amount_minor": s.ucp_checkout_amount_minor,
        "ucp_merchant_quote": s.ucp_merchant_quote,
        "ucp_checkout_links": s.ucp_checkout_links,
        "ucp_checkout_policies": s.ucp_checkout_policies,
        "discover_location_radius_km": s.discover_location_radius_km,
        "buyer_source": s.buyer_source,
        "buyer": {"name": s.buyer.name, "email": s.buyer.email, "phone": s.buyer.phone},
        "booking": {
            "id": s.booking.id,
            "checkout_path": s.booking.checkout_path,
            "payment_context": s.booking.payment_context,
            "action_expires_at": s.booking.action_expires_at,
        },
        "payment": {
            "status": s.payment.status,
            "spend_request_id": s.payment.spend_request_id,
            "order_id": s.payment.order_id,
            "spend_approval_surfaced": s.payment.spend_approval_surfaced,
            "spend_approval_user_surfaced": s.payment.spend_approval_user_surfaced,
        },
        "flow_timings": dict(s.flow_timings),
        "human_gate_timings": dict(s.human_gate_timings),
    }


# ---------------------------------------------------------------------------
# prereq
# ---------------------------------------------------------------------------


prereq_app = typer.Typer(help="Prerequisite checks before flow can start.")
app.add_typer(prereq_app, name="prereq")


def _probe_registry(url: str) -> bool:
    """Return True if the USP registry endpoint is reachable."""
    try:
        resp = httpx.get(url, timeout=coerce_timeout(5.0))
        # Any HTTP response (even 4xx) proves the registry host is reachable.
        return resp.status_code < 500
    except Exception:
        return False


@prereq_app.command("check")
def prereq_check() -> None:
    """Verify the USP registry is reachable (path-agnostic)."""
    from booking_agent.registry_url import DEFAULT_REGISTRY_URL

    session = must_load()
    registry_url = (settings.get("USP_REGISTRY_URL") or DEFAULT_REGISTRY_URL).strip()

    if not _probe_registry(registry_url):
        emit_error(
            session.flow_phase,
            code="registry_unreachable",
            message=f"USP registry not reachable at {registry_url}.",
            next_command="booking-agent flow prereq check",
        )
        raise typer.Exit(code=1)

    transition(session, to="PREREQ_OK")
    save_session(session)
    emit(_agent_out(
        session,
        prompt="USP registry reachable. Select a business next.",
        next_command="booking-agent flow discover",
        data={"registry_url": registry_url},
    ))


@prereq_app.command("check-ucp")
def prereq_check_ucp() -> None:
    """Validate UCP-native checkout prerequisites."""
    session = must_load()
    try:
        run_prereq_check_ucp()
    except PrereqFailure as err:
        emit_error(
            session.flow_phase,
            code=err.code,
            message=err.message,
            next_command="booking-agent flow prereq check-ucp",
        )
        raise typer.Exit(code=1)
    emit(_agent_out(
        session,
        prompt="UCP-native checkout prerequisites satisfied.",
        data={"platform_profile_url": platform_profile_url()},
    ))


# ---------------------------------------------------------------------------
# discover / business
# ---------------------------------------------------------------------------


def _format_price_display(amount_minor: Optional[int], currency: Optional[str]) -> Optional[str]:
    """Human-readable price for agent-facing JSON (USP amounts are minor currency units)."""
    if amount_minor is None or not currency:
        return None
    from booking_agent.backend import CURRENCY_SYMBOLS, minor_to_decimal

    value = minor_to_decimal(int(amount_minor), currency)
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), "")
    if symbol:
        return f"{symbol}{value:.2f}"
    return f"{value:.2f} {currency.upper()}"


search_area_app = typer.Typer(
    help="Confirm the search area or service address with the user before it is used.",
)
app.add_typer(search_area_app, name="search-area")

_AREA_PURPOSES = {"search": "search", "service-address": "service_address"}


def _area_purpose_option(raw: Optional[str], session: Session) -> str:
    from ..search_area import PURPOSE_SEARCH

    if raw is None:
        pending = session.pending_area or {}
        return pending.get("purpose") or PURPOSE_SEARCH
    purpose = _AREA_PURPOSES.get(raw.strip().lower())
    if purpose is None:
        emit_error(
            session.flow_phase,
            code="search_area_purpose_invalid",
            message="--purpose must be search or service-address.",
            next_command="booking-agent flow search-area propose",
        )
        raise typer.Exit(code=1)
    return purpose


def _area_flag(purpose: str) -> str:
    from ..search_area import PURPOSE_SERVICE_ADDRESS

    return " --purpose service-address" if purpose == PURPOSE_SERVICE_ADDRESS else ""


def _refuse_area_change_after_booking(session: Session) -> None:
    if session.booking.id:
        emit_error(
            session.flow_phase,
            code="booking_already_created",
            message=(
                "This booking already exists, so its address can no longer change. "
                "Start a new booking to use a different address."
            ),
            next_command="booking-agent flow status",
        )
        raise typer.Exit(code=1)


def _emit_area_input_needed(
    session: Session, purpose: str, *, reason: str, data: Optional[dict] = None,
) -> None:
    from ..search_area import PURPOSE_SERVICE_ADDRESS

    if purpose == PURPOSE_SERVICE_ADDRESS:
        ask = (
            "This service comes to the customer, so the business needs the exact street "
            "address. Ask the user for the full street address where the service should "
            "happen (street, number, city)."
        )
        awaiting = "service_address_input"
    else:
        ask = (
            "Ask the user which address or area to search near (a street address, "
            "neighborhood, or city)."
        )
        awaiting = "search_area_input"
    emit(_agent_out(
        session,
        awaiting_user=awaiting,
        prompt=(
            f"{reason} {ask} STOP for their answer. Then run next_command with exactly "
            "what they typed. Never guess, complete, or invent an address."
        ),
        next_command=(
            f"booking-agent flow search-area set --address '<what the user typed>'"
            f"{_area_flag(purpose)}"
        ),
        data=data or {},
    ))


def _emit_area_proposal(
    session: Session, proposal: Dict[str, Any], *, others: Optional[List[Dict[str, Any]]] = None,
) -> None:
    from ..search_area import PURPOSE_SERVICE_ADDRESS, SOURCE_TYPED, public_view

    purpose = proposal.get("purpose")
    view = public_view(proposal)
    if purpose == PURPOSE_SERVICE_ADDRESS:
        awaiting = "service_address_confirmation"
        question = (
            "This service comes to the customer. Ask in one question whether the business "
            "should come to data.proposed_area.label."
        )
    else:
        awaiting = "search_area_confirmation"
        question = (
            "Ask in one question whether to search near data.proposed_area.label, or "
            "somewhere else. Search nothing until they confirm."
        )
    if proposal.get("source") == SOURCE_TYPED:
        question = (
            "The address the user typed was matched by the map lookup as "
            "data.proposed_area.matched. Show that match and ask whether it is right. "
            + question
        )
    alternatives = (
        " If they pick another saved address from data.other_addresses, run "
        "`booking-agent flow search-area set --link-address-id <id>"
        f"{_area_flag(purpose)}`."
        if others else ""
    )
    data: Dict[str, Any] = {"proposed_area": view, "purpose": purpose}
    if others:
        data["other_addresses"] = others
    emit(_agent_out(
        session,
        awaiting_user=awaiting,
        prompt=(
            f"{question} STOP for their answer. If they agree, run next_command.{alternatives} "
            "If they give a different address or area, run `booking-agent flow search-area "
            f"set --address '<exactly what they typed>'{_area_flag(purpose)}`. Never guess, "
            "complete, or invent an address."
        ),
        next_command="booking-agent flow search-area confirm --user-confirmed",
        data=data,
    ))


def _link_signed_out() -> bool:
    """True only when link-cli positively reports no sign-in."""
    try:
        return not get_auth_status().authenticated
    except Exception:
        return False


def _propose_area(session: Session, purpose: str, *, link_cli_bin: Optional[str] = None) -> None:
    """Geocode the default Link address and ask the user to confirm it (or ask for one)."""
    from ..geocode import GeocodeError
    from ..link_cli import LinkCliShippingAddressError, retrieve_shipping_addresses
    from ..search_area import link_address_options, link_entry_label, proposal_from_link_entry

    try:
        entries = retrieve_shipping_addresses(link_cli_bin=link_cli_bin)
    except LinkCliShippingAddressError as err:
        if err.code == "link_cli_failed" and _link_signed_out():
            session.link_auth_gate_completed = False
            save_session(session)
            emit_error(
                session.flow_phase,
                code="link_auth_gate_not_completed",
                message=(
                    "The user is not signed in to Link, so their saved addresses cannot be "
                    "read. Run next_command now without asking the user, then re-run "
                    "search-area propose."
                ),
                next_command=LINK_AUTH_CONNECT,
            )
            raise typer.Exit(code=1)
        entries = []
    options = link_address_options(entries)
    chosen = next((e for e in entries if e.get("is_default") is True), None)
    if chosen is None and entries:
        chosen = entries[0]
    if chosen is None or not link_entry_label(chosen):
        session.pending_area = {"purpose": purpose}
        save_session(session)
        _emit_area_input_needed(
            session, purpose, reason="There is no saved address in the user's Link wallet.",
        )
        return
    try:
        proposal = proposal_from_link_entry(chosen, purpose=purpose)
    except GeocodeError as err:
        session.pending_area = {"purpose": purpose}
        save_session(session)
        _emit_area_input_needed(
            session, purpose,
            reason=(
                "The saved Link address could not be located on the map "
                f"({err.code}). Tell the user."
            ),
            data={"saved_address": link_entry_label(chosen), "geocode_error": err.code},
        )
        return
    session.pending_area = proposal
    save_session(session)
    others = [o for o in options if o["id"] != chosen.get("id")]
    _emit_area_proposal(session, proposal, others=others)


@search_area_app.command("propose")
def search_area_propose(
    purpose_raw: Optional[str] = typer.Option(
        None, "--purpose", help="search (default) or service-address",
    ),
    link_cli_bin: Optional[str] = typer.Option(None, "--link-cli-bin", hidden=True),
) -> None:
    """Propose the default Link address as the search area or service address."""
    session = must_load()
    _refuse_area_change_after_booking(session)
    purpose = _area_purpose_option(purpose_raw or "search", session)
    _propose_area(session, purpose, link_cli_bin=link_cli_bin)


@search_area_app.command("set")
def search_area_set(
    link_address_id: Optional[str] = typer.Option(
        None, "--link-address-id", help="Id of a saved Link address the user picked",
    ),
    address: Optional[str] = typer.Option(
        None, "--address", help="Address or area exactly as the user typed it",
    ),
    purpose_raw: Optional[str] = typer.Option(
        None, "--purpose", help="search or service-address (default: keep the current one)",
    ),
    link_cli_bin: Optional[str] = typer.Option(None, "--link-cli-bin", hidden=True),
) -> None:
    """Geocode another address and show the match for the user to confirm."""
    from ..geocode import GeocodeError
    from ..link_cli import LinkCliShippingAddressError, retrieve_shipping_addresses
    from ..search_area import proposal_from_link_entry, proposal_from_text

    session = must_load()
    _refuse_area_change_after_booking(session)
    purpose = _area_purpose_option(purpose_raw, session)
    if bool(link_address_id) == bool(address and address.strip()):
        emit_error(
            session.flow_phase,
            code="search_area_input_invalid",
            message="Pass exactly one of --link-address-id or --address.",
            next_command="booking-agent flow search-area set --address '<what the user typed>'",
        )
        raise typer.Exit(code=1)
    try:
        if link_address_id:
            try:
                entries = retrieve_shipping_addresses(link_cli_bin=link_cli_bin)
            except LinkCliShippingAddressError as err:
                emit_error(
                    session.flow_phase, code=err.code, message="Could not read Link addresses.",
                    next_command="booking-agent flow search-area set --address '<what the user typed>'",
                )
                raise typer.Exit(code=1)
            entry = next((e for e in entries if e.get("id") == link_address_id), None)
            if entry is None:
                emit_error(
                    session.flow_phase,
                    code="link_address_not_found",
                    message="No saved Link address has that id.",
                    next_command=f"booking-agent flow search-area propose{_area_flag(purpose)}",
                )
                raise typer.Exit(code=1)
            proposal = proposal_from_link_entry(entry, purpose=purpose)
        else:
            proposal = proposal_from_text(address or "", purpose=purpose)
    except GeocodeError as err:
        session.pending_area = {"purpose": purpose}
        save_session(session)
        _emit_area_input_needed(
            session, purpose,
            reason=(
                "The map lookup could not find that address "
                f"({err.code}). Tell the user and ask them to check it."
            ),
            data={"geocode_error": err.code},
        )
        return
    session.pending_area = proposal
    save_session(session)
    _emit_area_proposal(session, proposal)


@search_area_app.command("confirm")
def search_area_confirm(
    user_confirmed: bool = typer.Option(
        False, "--user-confirmed",
        help="The user explicitly confirmed the proposed address or area.",
    ),
) -> None:
    """Save the proposed area after the user confirmed it."""
    from ..search_area import PURPOSE_SERVICE_ADDRESS, public_view

    session = must_load()
    _refuse_area_change_after_booking(session)
    proposal = session.pending_area
    if not proposal or "label" not in proposal:
        emit_error(
            session.flow_phase,
            code="search_area_not_proposed",
            message="There is no proposed address to confirm.",
            next_command="booking-agent flow search-area propose",
        )
        raise typer.Exit(code=1)
    purpose = proposal.get("purpose")
    if not user_confirmed:
        emit_error(
            session.flow_phase,
            code="search_area_not_user_confirmed",
            message=(
                "Ask the user to confirm data.proposed_area.label first, then re-run with "
                "--user-confirmed."
            ),
            next_command="booking-agent flow search-area confirm --user-confirmed",
            data={"proposed_area": public_view(proposal)},
        )
        raise typer.Exit(code=1)
    if purpose == PURPOSE_SERVICE_ADDRESS and not proposal.get("street_level"):
        session.pending_area = {"purpose": purpose}
        save_session(session)
        _emit_area_input_needed(
            session, purpose,
            reason=(
                f"{proposal.get('label')!s} is an area, not a street address, so the "
                "business would not know where to go."
            ),
        )
        return
    confirmed = {k: v for k, v in proposal.items() if k not in ("purpose", "address")}
    confirmed["confirmed"] = True
    if proposal.get("street_level") and proposal.get("address"):
        session.service_address = dict(proposal["address"])
        session.service_address_label = proposal.get("label")
    elif purpose != PURPOSE_SERVICE_ADDRESS:
        session.service_address = None
        session.service_address_label = None
    if purpose != PURPOSE_SERVICE_ADDRESS:
        session.search_area = confirmed
    session.pending_area = None
    resume = session.area_resume_command
    session.area_resume_command = None
    save_session(session)
    if resume:
        next_cmd = resume
        prompt = "Address confirmed. Run next_command to continue."
    else:
        next_cmd = "booking-agent flow discover --radius-km <km>"
        prompt = (
            "Search area confirmed. Run next_command with the radius for this kind of "
            "service. Do not pass --location; the CLI uses the confirmed area."
        )
    emit(_agent_out(
        session,
        prompt=prompt,
        next_command=next_cmd,
        data={
            "confirmed_area": public_view(proposal),
            "service_address": session.service_address_label,
        },
    ))


@app.command("discover")
def discover(
    query: Optional[str] = typer.Option(
        None,
        "--query",
        "-q",
        help="Registry search query (defaults to the session user_request from flow init)",
    ),
    location: Optional[str] = typer.Option(
        None,
        "--location",
        hidden=True,
        help="Scripts only: latitude,longitude. Agents confirm an area with `flow search-area`.",
    ),
    radius_km: Optional[float] = typer.Option(
        None,
        "--radius-km",
        help="Search radius in km around the confirmed search area",
    ),
    vertical: Optional[List[str]] = typer.Option(
        None,
        "--vertical",
        hidden=True,
        help="Scripts only: repeatable registry vertical filter.",
    ),
    desired_at: Optional[List[str]] = typer.Option(
        None, "--desired-at", help="Repeatable RFC3339 preferred moment"
    ),
    desired_from: Optional[List[str]] = typer.Option(
        None, "--desired-from", help="Repeatable RFC3339 open lower bound"
    ),
    desired_range: Optional[List[str]] = typer.Option(
        None,
        "--desired-range",
        help="Repeatable <startRfc3339>,<endRfc3339> preferred window",
    ),
    prefer_sooner_availability_slots: Optional[bool] = typer.Option(
        None,
        "--prefer-sooner-availability-slots/--no-prefer-sooner-availability-slots",
        help="Explicitly enable or disable earlier-opening preference",
    ),
) -> None:
    """Search the USP registry for services."""
    session = must_load()
    try:
        require_phase(session, "PREREQ_OK", "BUSINESS_SELECTED", "INIT",
                      code="bad_phase_for_discover")
    except GateError as e:
        raise _handle_gate(session, e)

    effective_query = (query or "").strip() or (session.user_request or "").strip()
    if not effective_query:
        emit_error(
            session.flow_phase,
            code="discover_query_missing",
            message=(
                "Registry search needs a query. Pass --query or set user_request via "
                "`booking-agent flow init --user-request`."
            ),
        )
        raise typer.Exit(code=1)

    loc = None
    area = session.search_area if (session.search_area or {}).get("confirmed") else None
    if not (location and location.strip()) and area is not None:
        if radius_km is None:
            emit_error(
                session.flow_phase,
                code="discover_radius_required",
                message="Searching near the confirmed area needs --radius-km.",
                next_command="booking-agent flow discover --radius-km <km>",
            )
            raise typer.Exit(code=1)
        from booking_agent.models.platform import ServiceSearchLocation
        from booking_agent.models.wire import Coordinates

        loc = ServiceSearchLocation(
            coordinates=Coordinates(lat=float(area["lat"]), lng=float(area["lng"])),
            radius_km=radius_km,
        )
    elif not (location and location.strip()) and radius_km is not None:
        emit_error(
            session.flow_phase,
            code="search_area_unconfirmed",
            message=(
                "A place-based search needs an area the user confirmed. Propose their "
                "address first and ask them."
            ),
            next_command="booking-agent flow search-area propose",
        )
        raise typer.Exit(code=1)
    if location and location.strip():
        try:
            from booking_agent.headless import parse_service_search_location
            from booking_agent.models.platform import ServiceSearchLocation

            parsed = parse_service_search_location(location)
            if radius_km is not None:
                loc = ServiceSearchLocation(
                    coordinates=parsed.coordinates,
                    radius_km=radius_km,
                )
            else:
                loc = parsed
        except ValueError as exc:
            emit_error(
                session.flow_phase,
                code="discover_location_invalid",
                message=str(exc),
                next_command=(
                    "booking-agent flow discover --location <latitude>,<longitude>"
                ),
            )
            raise typer.Exit(code=1)

    try:
        from booking_agent.headless import build_desired_service_time_preferences

        time_preferences = build_desired_service_time_preferences(
            desired_at=desired_at,
            desired_from=desired_from,
            desired_range=desired_range,
        )
    except ValueError as exc:
        emit_error(
            session.flow_phase,
            code="discover_time_preference_invalid",
            message=str(exc),
        )
        raise typer.Exit(code=1)

    try:
        from booking_agent.headless import parse_service_verticals

        verticals = parse_service_verticals(vertical)
    except ValueError as exc:
        emit_error(
            session.flow_phase,
            code="discover_vertical_invalid",
            message=str(exc),
        )
        raise typer.Exit(code=1)

    backend = resolve_backend()

    async def _go() -> list:
        from booking_agent.headless import build_search_intent, discover_services

        intent = build_search_intent(
            query=effective_query,
            location=loc,
            verticals=verticals,
            desired_service_time_ranges=time_preferences or None,
            prefer_sooner_availability_slots=prefer_sooner_availability_slots,
        )
        return intent, await discover_services(backend, intent)

    try:
        intent, services = asyncio.run(_go())
    except RegistrySearchRejectedError as e:
        emit_error(
            session.flow_phase,
            code="registry_search_rejected",
            message=f"The registry rejected the search: {e.message}",
            data={"registry_error": e.as_dict()},
        )
        raise typer.Exit(code=1)
    except Exception as e:
        raise _emit_flow_http_error(
            session.flow_phase, e, default_code="discover_failed",
        )

    from ..flow_timings import apply_pending_timings

    apply_pending_timings(session)

    from booking_agent.backend.live import DEFAULT_REGISTRY_SEARCH_LIMIT
    from booking_agent.registry_url import DEFAULT_REGISTRY_URL, registry_search_post_url
    from booking_agent.rest_client import build_registry_search_body

    registry_url = (settings.get("USP_REGISTRY_URL") or DEFAULT_REGISTRY_URL).strip()
    session.discover_registry_url = registry_search_post_url(registry_url)
    session.discover_registry_request_body = build_registry_search_body(
        intent.query,
        location=intent.location,
        verticals=intent.verticals or None,
        categories=intent.categories or None,
        price_range=intent.price_range,
        duration_range=intent.duration_range,
        desired_service_time_ranges=intent.desired_service_time_ranges,
        prefer_sooner_availability_slots=intent.prefer_sooner_availability_slots,
        limit=DEFAULT_REGISTRY_SEARCH_LIMIT,
    )

    from ..catalog import (
        discover_display_line,
        discover_summary_from_service,
        sort_discover_summaries_by_rank,
    )
    from ..geo_eligibility import distance_facts_for_session, geo_refusal_for_session

    session.discover_location_lat = loc.coordinates.lat if loc else None
    session.discover_location_lng = loc.coordinates.lng if loc else None
    session.discover_location_radius_km = loc.radius_km if loc else None

    results_summary = []
    for rank, service in enumerate(services):
        summary = discover_summary_from_service(service)
        if summary.get("search_rank") is None:
            summary["search_rank"] = rank
        refusal = geo_refusal_for_session(session, summary)
        if refusal is not None:
            continue
        facts = distance_facts_for_session(session, summary)
        if facts:
            summary["location_facts"] = facts
        summary["display_line"] = discover_display_line(summary)
        results_summary.append(summary)
    results_summary = sort_discover_summaries_by_rank(results_summary)
    session.discover_query = effective_query
    session.discover_services = results_summary
    save_session(session)
    discover_data: dict = {
        "services": results_summary,
        "discover_query": effective_query,
    }
    if intent.desired_service_time_ranges:
        discover_data["desired_service_time_ranges"] = [
            preference.model_dump(by_alias=True, exclude_none=True)
            for preference in intent.desired_service_time_ranges
        ]
    if intent.prefer_sooner_availability_slots is not None:
        discover_data["prefer_sooner_availability_slots"] = (
            intent.prefer_sooner_availability_slots
        )
    if loc is not None:
        discover_location: dict = {
            "lat": loc.coordinates.lat,
            "lng": loc.coordinates.lng,
        }
        if loc.radius_km is not None:
            discover_location["radius_km"] = loc.radius_km
        discover_data["discover_location"] = discover_location
        if area is not None and not location:
            discover_data["search_area"] = area.get("label")
    emit(_agent_out(
        session,
        awaiting_user="business_choice",
        prompt=(
            "Present every service in data.services in the exact order returned (option 1 "
            "first), one numbered line each showing that row's display_line verbatim "
            "(service, business, price, duration, where), and wait for the user to pick "
            "one. Never omit, filter, or reorder results, and never drop the business "
            "name. Present direct matches plainly. When a result's returned data "
            "objectively mismatches the request (for example channel.type "
            "at_business_location when the user needs a home visit, or a clearly "
            "different service name such as heating or duct cleaning for an AC repair "
            "request), keep it in place and label it as a less-direct alternative with "
            "the specific mismatch. Do not infer categories or compute your own "
            "relevance score. Price, duration, and visit type come from the business's "
            "own catalog when field_sources says live_catalog. When a row has "
            "discovery_mismatches, show the live value and add that the search listing "
            "said otherwise; never guess which one is right. If the user picks a result "
            "you labeled less-direct, pass that label with --less-direct."
        ),
        next_command=(
            "booking-agent flow business use --profile-url <selected profile_url> "
            "--service-id <selected service_id> --name '<selected business_name>' "
            "[--less-direct '<label shown>']"
        ),
        data=discover_data,
    ))


business_app = typer.Typer(help="Business selection.")
app.add_typer(business_app, name="business")


def _valid_iana_timezone(value: Any) -> Optional[str]:
    return tz_context.valid_iana_timezone(value)


def _discover_row(session: Session, service_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not service_id:
        return None
    return next(
        (row for row in session.discover_services or [] if row.get("service_id") == service_id),
        None,
    )


def _business_use_command(
    profile_url: str,
    service_id: Optional[str],
    name: Optional[str],
    *,
    less_direct: Optional[str] = None,
    flags: Optional[List[str]] = None,
) -> str:
    parts = ["booking-agent flow business use", "--profile-url", shlex.quote(profile_url)]
    if service_id:
        parts += ["--service-id", shlex.quote(service_id)]
    if name:
        parts += ["--name", shlex.quote(name)]
    if less_direct:
        parts += ["--less-direct", shlex.quote(less_direct)]
    parts += list(flags or [])
    return " ".join(parts)


def _registry_service_timezone(session: Session, service_id: Optional[str]) -> Optional[str]:
    if not service_id:
        return None
    for row in session.discover_services or []:
        if row.get("service_id") != service_id:
            continue
        result = row.get("registry_search_result")
        return result.get("timezone") if isinstance(result, dict) else None
    return None


def _resolve_business_timezone(profile_tz: Any, service_tz: Any) -> Dict[str, Any]:
    """Pick the authoritative business zone: business profile first, registry service second.

    Only explicit business-published zones count. When neither source names a
    zone the result is ``None`` and the availability response's
    ``business_timezone`` may fill it later (USP 4.3.1). Raises
    ``PrereqFailure`` only when a published zone is not a valid IANA zone.
    """
    profile_valid = _valid_iana_timezone(profile_tz)
    service_valid = _valid_iana_timezone(service_tz)
    business_tz = profile_valid or service_valid
    if business_tz is None:
        provided = [v for v in (profile_tz, service_tz) if v not in (None, "")]
        if provided:
            raise PrereqFailure(
                "business_timezone_invalid",
                "The business published a timezone that is not a valid IANA zone "
                f"({', '.join(repr(str(v)[:64]) for v in provided)}). Availability cannot be "
                "shown without the business's local timezone. Pick a different business.",
            )
        return {
            "business_timezone": None,
            "business_timezone_source": None,
            "service_timezone": None,
        }
    out: Dict[str, Any] = {
        "business_timezone": business_tz,
        "business_timezone_source": (
            "business_profile" if profile_valid else "registry_service"
        ),
        "service_timezone": service_valid,
    }
    if profile_valid and service_valid and profile_valid != service_valid:
        out["timezone_discrepancy"] = {
            "business_timezone": profile_valid,
            "service_timezone": service_valid,
        }
    return out


@business_app.command("use")
def business_use(
    profile_url: str = typer.Option(..., "--profile-url", help="Business USP profile URL"),
    service_id: Optional[str] = typer.Option(
        None,
        "--service-id",
        help="Service id from registry search; fetches live catalog before calendar gate (USP §6.3)",
    ),
    name: Optional[str] = typer.Option(None, help="Optional human-readable business name"),
    less_direct: Optional[str] = typer.Option(
        None,
        "--less-direct",
        help="The less-direct label shown to the user for this result (e.g. 'duct cleaning, not AC repair').",
    ),
    mismatch_confirmed: bool = typer.Option(
        False,
        "--mismatch-confirmed",
        help="The user confirmed they want this less-direct result despite the stated mismatch.",
    ),
    details_confirmed: bool = typer.Option(
        False,
        "--details-confirmed",
        help="The user confirmed the live catalog details that differ from the search result.",
    ),
) -> None:
    """Select a business by profile URL.

    Direct-profile selection uses the same gates as registry discovery: calendar
    must still be asked before availability.
    """
    session = must_load()
    if less_direct and less_direct.strip() and not mismatch_confirmed:
        label = less_direct.strip()
        emit(_agent_out(
            session,
            awaiting_user="intent_mismatch_confirmation",
            prompt=(
                "The user picked a result you labeled as a less-direct match. Before "
                "selecting it, ask one plain question that restates the mismatch (for "
                f"example: \"This one is {label}. Do you still want it?\") and STOP. If they "
                "agree, re-run next_command. Otherwise show the other results again."
            ),
            next_command=_business_use_command(
                profile_url, service_id, name, less_direct=label,
                flags=["--mismatch-confirmed", *(["--details-confirmed"] if details_confirmed else [])],
            ),
            data={"less_direct": label, "service_id": service_id},
        ))
        return
    session.selection_mismatch = (less_direct or "").strip() or None

    async def _resolve():
        from booking_agent.ucp_profile import fetch_and_resolve_business_profile

        return await fetch_and_resolve_business_profile(profile_url)

    try:
        profile, uctx = asyncio.run(_resolve())
    except Exception as e:
        raise _emit_flow_http_error(
            session.flow_phase, e, default_code="profile_resolve_failed",
        )

    try:
        timezone_facts = _resolve_business_timezone(
            profile.business.timezone,
            _registry_service_timezone(session, service_id),
        )
    except PrereqFailure as err:
        emit_error(
            session.flow_phase,
            code=err.code,
            message=err.message,
            next_command="booking-agent flow discover",
        )
        raise typer.Exit(code=1)

    live_service_summary_data: Optional[dict] = None
    if service_id:
        backend = resolve_backend()

        async def _catalog():
            from ..catalog import live_service_summary

            live = await backend.get_service(profile, service_id)
            if live.id != service_id:
                raise RuntimeError(
                    f"Live catalog id {live.id!r} does not match requested service_id {service_id!r}"
                )
            return live_service_summary(live)

        try:
            live_service_summary_data = asyncio.run(_catalog())
        except Exception as e:
            raise _emit_flow_http_error(
                session.flow_phase, e, default_code="catalog_fetch_failed",
            )
        from ..catalog import presented_service_changes

        changes = presented_service_changes(
            _discover_row(session, service_id), live_service_summary_data,
        )
        if changes and not details_confirmed:
            emit_error(
                session.flow_phase,
                code="service_details_changed",
                message=(
                    "The business's live catalog differs from the search result the user "
                    "picked. Tell the user in plain words what changed (each entry in "
                    "data.changes, the search value then the current value) and ask whether "
                    "they still want this service. STOP. If they agree, run next_command. "
                    "Otherwise show the other results again."
                ),
                next_command=_business_use_command(
                    profile_url, service_id, name,
                    less_direct=session.selection_mismatch,
                    flags=[
                        *(["--mismatch-confirmed"] if session.selection_mismatch else []),
                        "--details-confirmed",
                    ],
                ),
                data={"changes": changes, "live_service": live_service_summary_data},
            )
            raise typer.Exit(code=1)
        from ..catalog import classify_payment_route, payment_timing_refusal

        classified = classify_payment_route(live_service_summary_data)
        session.payment_timing = classified["timing"]
        session.payment_route = classified["route"]
        timing_refusal = payment_timing_refusal(live_service_summary_data)
        if timing_refusal is not None:
            emit_error(
                session.flow_phase,
                code=timing_refusal["code"],
                message=timing_refusal["message"],
            )
            raise typer.Exit(code=1)
        session.service_id = service_id
        session.live_service = live_service_summary_data
        from ..geo_eligibility import distance_facts_for_session, geo_refusal_for_session

        refusal = geo_refusal_for_session(session, live_service_summary_data)
        if refusal is not None:
            emit_error(
                session.flow_phase,
                code=refusal["code"],
                message=refusal["message"],
                data=refusal,
            )
            raise typer.Exit(code=1)
        facts = distance_facts_for_session(session, live_service_summary_data)
        if facts:
            live_service_summary_data["location_facts"] = facts
            session.live_service = live_service_summary_data
        from ..catalog import channel_type
        from ..search_area import PURPOSE_SERVICE_ADDRESS

        if channel_type(live_service_summary_data) == "at_buyer_location" and not session.service_address:
            session.area_resume_command = _business_use_command(
                profile_url, service_id, name,
                less_direct=session.selection_mismatch,
                flags=[
                    *(["--mismatch-confirmed"] if session.selection_mismatch else []),
                    *(["--details-confirmed"] if details_confirmed else []),
                ],
            )
            save_session(session)
            _propose_area(session, PURPOSE_SERVICE_ADDRESS)
            return

    discovered = _discover_row(session, service_id) or {}
    session.business = Business(
        profile_url=profile_url,
        rest_endpoint=profile.rest_endpoint,
        name=name or discovered.get("business_name") or profile.business.name,
        timezone=timezone_facts["business_timezone"],
    )
    session.service_timezone = timezone_facts["service_timezone"]
    session.timezone_difference_shown = False
    direct_route = session.payment_route in ("free", "at_service")
    # Direct bookings never open checkout, so a USP-only profile with no
    # dev.ucp.shopping binding must not be asked for a shopping endpoint.
    if direct_route:
        supported_modes = []
        mode_reason = ""
    else:
        supported_modes, mode_reason = supported_checkout_modes_from_profile(uctx)
        if not supported_modes:
            emit_error(
                session.flow_phase,
                code="ucp_native_required",
                message=mode_reason,
                next_command="booking-agent flow discover",
            )
            raise typer.Exit(code=1)
    session.supported_checkout_modes = list(supported_modes)
    if direct_route:
        session.checkout_mode = "direct"
        if "direct" not in session.supported_checkout_modes:
            session.supported_checkout_modes.append("direct")
        session.checkout_prereq_completed = True
        mode_reason = (
            "This service is booked directly. The quoted price is due at the appointment."
            if session.payment_route == "at_service"
            else "This service does not require payment."
        )
    else:
        try:
            session.checkout_mode = select_checkout_mode(supported_modes)
        except PrereqFailure as err:
            emit_error(
                session.flow_phase,
                code=err.code,
                message=err.message,
                next_command="booking-agent flow discover",
            )
            raise typer.Exit(code=1)
        session.checkout_prereq_completed = False
        next_cmd = "booking-agent flow use-ucp-checkout-mode"
    if uctx is not None and not direct_route:
        session.ucp_checkout_rest_base = uctx.ucp_rest_endpoint()
    else:
        session.ucp_checkout_rest_base = None
    session.ucp_checkout_session_id = None
    session.ucp_checkout_amount_minor = None
    session.ucp_spt_network_id = None
    transition(session, to="BUSINESS_SELECTED")
    session.calendar_gate_completed = False
    session.calendar_state = "unset"
    if not direct_route:
        session.checkout_prereq_completed = False
    save_session(session)

    data_out: dict = {
        "business": _session_data(session)["business"],
        "checkout_mode": session.checkout_mode,
        "checkout_mode_reason": mode_reason,
        "supported_checkout_modes": list(session.supported_checkout_modes),
        "payment_route": session.payment_route,
        "payment_timing": session.payment_timing,
        "business_timezone": timezone_facts["business_timezone"],
        "business_timezone_source": timezone_facts["business_timezone_source"],
    }
    if "timezone_discrepancy" in timezone_facts:
        data_out["timezone_discrepancy"] = timezone_facts["timezone_discrepancy"]
    if session.ucp_checkout_rest_base:
        data_out["ucp_rest_base"] = session.ucp_checkout_rest_base
    if live_service_summary_data is not None:
        data_out["live_service"] = live_service_summary_data

    if direct_route:
        # Same gate as paid checkout: remembered preference, no-identity
        # auto-skip, or calendar ask. Do not record a user opt-out here.
        transition(session, to="CALENDAR_GATE")
        save_session(session)
        _emit_calendar_gate_entry(session, data=data_out)
        return

    emit(_agent_out(
        session,
        prompt=(
            "Checkout mode selected. Run the next_command to complete checkout "
            "prerequisites. Do not ask about Google Calendar yet."
        ),
        next_command=next_cmd,
        data=data_out,
    ))


@app.command("use-ucp-checkout-mode")
def use_ucp_checkout_mode() -> None:
    """UCP-native path orchestrator for the v2 booking flow."""
    session = must_load()
    try:
        require_business(session)
    except GateError as e:
        raise _handle_gate(session, e)
    try:
        run_prereq_check_ucp()
    except PrereqFailure as err:
        emit_error(
            session.flow_phase,
            code=err.code,
            message=err.message,
            next_command="booking-agent flow use-ucp-checkout-mode",
        )
        raise typer.Exit(code=1)
    session.checkout_mode = "ucp_native"
    session.checkout_prereq_completed = True
    transition(session, to="CALENDAR_GATE")
    save_session(session)
    _emit_calendar_gate_entry(
        session,
        data={
            "checkout_mode": session.checkout_mode,
            "platform_profile_url": platform_profile_url(),
            "ucp_rest_base": session.ucp_checkout_rest_base,
        },
    )


# ---------------------------------------------------------------------------
# link-auth gate
# ---------------------------------------------------------------------------


link_auth_app = typer.Typer(
    help="Link wallet sign-in (first step of every booking; also works with no session)."
)
app.add_typer(link_auth_app, name="link-auth")

LINK_AUTH_CONNECT = "booking-agent flow link-auth connect"
LINK_AUTH_POLL = "booking-agent flow link-auth poll"


def _link_auth_session() -> Optional[Session]:
    """Active non-terminal session, or None (sign-in still runs, nothing is recorded)."""
    session = load_session()
    if session is None or session.flow_phase in TERMINAL_PHASES:
        return None
    return session


def _link_auth_phase(session: Optional[Session]) -> str:
    return session.flow_phase if session is not None else "STORE"


def _link_auth_record(session: Optional[Session], completed: bool) -> None:
    if session is not None:
        session.link_auth_gate_completed = completed
        save_session(session)


def _require_link_cli(session: Optional[Session], retry: str) -> None:
    if shutil.which(default_link_cli_bin()) is None:
        emit_error(
            _link_auth_phase(session),
            code="link_cli_not_found",
            message=(
                f"`{default_link_cli_bin()}` not found on PATH. Install link-cli "
                "(https://github.com/stripe/link-cli) first."
            ),
            next_command=retry,
        )
        raise typer.Exit(code=1)


def _emit_link_authenticated(session: Optional[Session]) -> None:
    _link_auth_record(session, True)
    out = AgentOutput(
        phase="STORE",
        prompt="Link wallet authenticated. No booking in progress.",
        next_command=None,
        data={"authenticated": True},
    )
    if session is not None:
        out = _agent_out(
            session,
            prompt="Link wallet authenticated. Continue the booking flow without asking the user.",
            next_command=_link_auth_next_command(session),
            data={"authenticated": True},
        )
    emit(out)


def _link_auth_next_command(session: Session) -> str:
    """Best next command after Link wallet auth succeeds."""
    if session.flow_phase == "SLOT_SELECTED":
        return "booking-agent flow buyer resolve"
    if session.flow_phase == "BUYER_RESOLVED":
        return "booking-agent flow book"
    if session.flow_phase == "BOOKED":
        return (
            "booking-agent flow pay request --payment-method-id <id>"
        )
    if session.flow_phase == "PAYMENT_PENDING":
        sr = session.payment.spend_request_id
        if sr:
            return "booking-agent flow pay poll"
        return (
            "booking-agent flow pay request --payment-method-id <id>"
        )
    nxt = recommend_next(session)
    return nxt or "booking-agent flow status"


@link_auth_app.command("connect")
def link_auth_connect(
    client_name: str = typer.Option(
        "booking-agent",
        "--client-name",
        help="Name shown in the Link app when the user approves device access.",
    ),
    wait: bool = typer.Option(
        False, "--wait",
        help=(
            "Scripts/CI only: also block until the user approves. Interactive agents run "
            "link-auth poll in the background instead."
        ),
    ),
    poll_interval: float = typer.Option(
        2.0, "--poll-interval", help="Seconds between auth status polls with --wait."
    ),
    timeout: int = typer.Option(
        300, "--timeout", help="Max seconds to wait for approval with --wait."
    ),
) -> None:
    """Start Link sign-in and return the sign-in URL at once (then run link-auth poll)."""
    session = _link_auth_session()
    phase = _link_auth_phase(session)
    _require_link_cli(session, LINK_AUTH_CONNECT)

    try:
        status = get_auth_status()
    except Exception as e:
        emit_error(
            phase,
            code="link_auth_status_failed",
            message=str(e),
            next_command=LINK_AUTH_CONNECT,
        )
        raise typer.Exit(code=1)

    if status.authenticated:
        _emit_link_authenticated(session)
        return

    verification_url = status.verification_url
    phrase = status.phrase
    if not verification_url:
        try:
            login_status = initiate_auth_login(client_name=client_name)
        except Exception as e:
            emit_error(
                phase,
                code="link_auth_login_failed",
                message=str(e),
                next_command=LINK_AUTH_CONNECT,
            )
            raise typer.Exit(code=1)
        if login_status.authenticated:
            _emit_link_authenticated(session)
            return
        verification_url = login_status.verification_url
        phrase = login_status.phrase

    if not verification_url:
        emit_error(
            phase,
            code="link_auth_no_verification_url",
            message="link-cli did not return a sign-in URL.",
            next_command=LINK_AUTH_CONNECT,
        )
        raise typer.Exit(code=1)

    _link_auth_record(session, False)
    out = AgentOutput(phase=phase)
    if session is not None:
        out = _agent_out(session)
    out.awaiting_user = AWAITING_LINK_AUTH_APPROVAL
    out.user_message = verification_url
    out.prompt = (
        "Show user_message (the Link sign-in URL) to the user as the raw URL (never markdown "
        f"[label](url)) and ask them to sign in to Link. {LINK_AUTH_POLL_BACKGROUND_INSTRUCTION}"
    )
    out.next_command = LINK_AUTH_POLL
    out.data = {**out.data, "verification_url": verification_url, "phrase": phrase}
    emit(out)

    if wait:
        _poll_link_auth(session, poll_interval=poll_interval, timeout=timeout)


def _poll_link_auth(session: Optional[Session], *, poll_interval: float, timeout: int) -> None:
    phase = _link_auth_phase(session)
    try:
        poll_auth_until_authenticated(poll_interval=poll_interval, timeout=timeout)
    except TimeoutError:
        _link_auth_record(session, False)
        try:
            still_pending = bool(get_auth_status().verification_url)
        except Exception:
            still_pending = False
        emit_error(
            phase,
            code="link_auth_timeout",
            message=(
                "Link sign-in not received yet. The same sign-in link still works: start "
                "link-auth poll again in the background without re-posting the link."
                if still_pending else
                "Link sign-in not received in time and the link expired. Run link-auth "
                "connect for a fresh link, post it once, and start link-auth poll in the "
                "background."
            ),
            next_command=LINK_AUTH_POLL if still_pending else LINK_AUTH_CONNECT,
        )
        raise typer.Exit(code=1)
    except Exception as e:
        _link_auth_record(session, False)
        emit_error(
            phase,
            code="link_auth_poll_failed",
            message=str(e),
            next_command=LINK_AUTH_CONNECT,
        )
        raise typer.Exit(code=1)
    _emit_link_authenticated(session)


@link_auth_app.command("poll")
def link_auth_poll(
    poll_interval: float = typer.Option(
        2.0, "--poll-interval", help="Seconds between auth status polls."
    ),
    timeout: int = typer.Option(
        300, "--timeout", help="Max seconds to wait for the user to approve sign-in."
    ),
) -> None:
    """Block until the user approves the Link sign-in started by link-auth connect."""
    session = _link_auth_session()
    phase = _link_auth_phase(session)
    _require_link_cli(session, LINK_AUTH_POLL)

    try:
        status = get_auth_status()
    except Exception as e:
        emit_error(
            phase,
            code="link_auth_status_failed",
            message=str(e),
            next_command=LINK_AUTH_POLL,
        )
        raise typer.Exit(code=1)

    if status.authenticated:
        _emit_link_authenticated(session)
        return
    if not status.verification_url:
        emit_error(
            phase,
            code="link_auth_not_started",
            message="No Link sign-in is waiting for approval. Run link-auth connect first.",
            next_command=LINK_AUTH_CONNECT,
        )
        raise typer.Exit(code=1)

    _poll_link_auth(session, poll_interval=poll_interval, timeout=timeout)


# ---------------------------------------------------------------------------
# calendar gate
# ---------------------------------------------------------------------------


calendar_app = typer.Typer(help="Calendar gate (must complete before availability).")
app.add_typer(calendar_app, name="calendar")

# Session id passed to CustomerCalendarService (OAuth token cache key).
_CALENDAR_SESSION_ID = "cli"
# Google sign-in plus consent on a phone routinely takes longer than two minutes.
CALENDAR_APPROVAL_TIMEOUT_SECONDS = 600
# Below common agent tool-call limits, so a foreground poll still returns a result.
CALENDAR_POLL_WAIT_SECONDS = 120
_VALID_CALENDAR_MODES = frozenset({CALENDAR_MODE_FREEBUSY, CALENDAR_MODE_EVENTS})


def _complete_calendar_gate(session: Session, *, mode: str) -> None:
    complete_calendar_gate_with_preference(
        session,
        state="connected",
        mode=mode,
    )


_CALENDAR_MODE_LABELS = {
    CALENDAR_MODE_FREEBUSY: "conflict checking (free/busy only)",
    CALENDAR_MODE_EVENTS: "conflict checking and post-booking calendar events",
}
_CALENDAR_SKIP_SESSION_ONLY = "booking-agent flow calendar skip --session-only"
_CALENDAR_POLL = "booking-agent flow calendar poll"
_TOKEN_ENV_PLACEHOLDER = "<TOKEN_VARIABLE>"


def _connect_command(mode: str, route: Optional[str], token_env: Optional[str] = None) -> str:
    """The exact connect command for a route; the connector route always names its variable."""
    base = f"booking-agent flow calendar connect --mode {mode}"
    if route == ROUTE_CONNECTOR:
        return f"{base} --route {ROUTE_CONNECTOR} --token-env {token_env or _TOKEN_ENV_PLACEHOLDER}"
    if route == ROUTE_LOCAL_OAUTH:
        return f"{base} --route {ROUTE_LOCAL_OAUTH}"
    return base


def _session_route(session: Session) -> Tuple[Optional[str], Optional[str]]:
    if session.calendar_route:
        return session.calendar_route, session.calendar_token_env
    return remembered_route()


def _reconnect_route(status: dict) -> Tuple[Optional[str], Optional[str]]:
    """Route a stale connection can be renewed on, or (None, None) when none can."""
    route, token_env = remembered_route()
    if route == ROUTE_CONNECTOR and valid_token_env_name(token_env):
        return route, token_env
    if status.get("local_oauth_configured"):
        return ROUTE_LOCAL_OAUTH, None
    return None, None


def _emit_calendar_unavailable(session: Session, status: dict, mode: str, *, data: Optional[dict]) -> None:
    skip_calendar_gate_session_only(session)
    out = dict(data or {})
    out.update(status)
    out["calendar_state"] = "skipped"
    out["calendar_mode"] = CALENDAR_MODE_NONE
    out["calendar_checking_unavailable"] = True
    out["stale_calendar_mode"] = mode
    emit(_agent_out(
        session,
        prompt=(
            "The saved Google Calendar access is no longer valid and reconnecting is not "
            "available on this device, so calendar checking is unavailable for this "
            "booking. Tell the user that in one plain sentence, then continue. Ready to "
            "query availability."
        ),
        next_command="booking-agent flow availability",
        data=out,
    ))


def _emit_calendar_reconnect_if_stale(
    session: Session,
    status: dict,
    *,
    data: Optional[dict] = None,
) -> bool:
    """Offer reconnect-or-skip when a remembered connected mode lost its access."""
    mode = stale_remembered_calendar_mode(status)
    if not mode:
        return False
    route, token_env = _reconnect_route(status)
    if route is None:
        _emit_calendar_unavailable(session, status, mode, data=data)
        return True
    reconnect = _connect_command(mode, route, token_env)
    label = _CALENDAR_MODE_LABELS[mode]
    out = dict(data or {})
    out.update(status)
    out["remembered_calendar_mode"] = mode
    out["calendar_route"] = route
    out["scope_request"] = scope_request(mode)
    out["calendar_reconnect_choices"] = [
        {"choice": "reconnect", "mode": mode, "command": reconnect},
        {"choice": "continue_without_calendar", "command": _CALENDAR_SKIP_SESSION_ONLY},
    ]
    save_session(session)
    emit(_agent_out(
        session,
        awaiting_user="calendar_reconnect",
        prompt=(
            f"The user's saved Google Calendar connection for {label} has expired. Ask "
            "exactly two choices: (1) reconnect Google Calendar for the same use, or "
            "(2) continue this booking without calendar checking. Map (1) to "
            f"`{reconnect}` and (2) to `{_CALENDAR_SKIP_SESSION_ONLY}`. Do not offer the "
            "other calendar options and do not change the saved preference."
        ),
        next_command=reconnect,
        data=out,
    ))
    return True


def _emit_calendar_gate_entry(session: Session, *, data: Optional[dict] = None) -> None:
    """Advance to calendar gate, auto-applying a remembered preference when possible."""
    status = calendar_connection_status()
    remembered = try_apply_remembered_calendar_gate(session, status)
    out = dict(data or {})
    out["calendar_connected"] = status.get("connected", False)
    pref_state, pref_mode = get_calendar_preference()
    if pref_state and pref_mode:
        out["remembered_calendar_state"] = pref_state
        out["remembered_calendar_mode"] = pref_mode
    if remembered:
        out["remembered_preference_applied"] = True
        out["calendar_state"] = remembered.state
        out["calendar_mode"] = remembered.mode
        emit(_agent_out(
            session,
            prompt=remembered.prompt,
            next_command="booking-agent flow availability",
            data=out,
        ))
        return
    if _emit_calendar_reconnect_if_stale(session, status, data=out):
        return
    emit(_agent_out(
        session,
        prompt=(
            "Checkout ready. Run `booking-agent flow calendar ask` next. Do not ask the user "
            "about calendar options yourself; the CLI applies a saved preference or tells "
            "you when to ask."
        ),
        next_command="booking-agent flow calendar ask",
        data=out,
    ))


@calendar_app.command("status")
def calendar_status() -> None:
    """Report existing Google Calendar connection and granted scopes."""
    session = must_load()
    try:
        require_business(session)
        require_checkout_prereq(session)
    except GateError as e:
        raise _handle_gate(session, e)

    status = calendar_connection_status()
    pref_state, pref_mode = get_calendar_preference()
    route, token_env = remembered_route()
    status_prompt = (
        "Use this to see what Google Calendar access is already granted. "
        "If a remembered preference can be applied automatically, run calendar ask next."
    )
    if remembered_preference_applicable(status):
        status_prompt = (
            "Google Calendar is connected and your saved preference will be applied "
            "automatically. Run calendar ask to continue without asking the user again."
        )
    emit(_agent_out(
        session,
        prompt=status_prompt,
        next_command="booking-agent flow calendar ask",
        data={
            "calendar_gate_completed": session.calendar_gate_completed,
            "calendar_state": session.calendar_state,
            "calendar_mode": session.calendar_mode,
            "remembered_calendar_state": pref_state,
            "remembered_calendar_mode": pref_mode,
            "remembered_preference_applicable": remembered_preference_applicable(status),
            "calendar_route": session.calendar_route or route,
            "calendar_token_env": session.calendar_token_env or token_env,
            **status,
        },
    ))


@calendar_app.command("ask")
def calendar_ask() -> None:
    """Tell the agent to ask the user about calendar integration options."""
    session = must_load()
    try:
        require_business(session)
        require_checkout_prereq(session)
    except GateError as e:
        raise _handle_gate(session, e)

    status = calendar_connection_status()
    remembered = try_apply_remembered_calendar_gate(session, status)
    if remembered:
        emit(_agent_out(
            session,
            prompt=remembered.prompt,
            next_command="booking-agent flow availability",
            data={
                "remembered_preference_applied": True,
                "calendar_state": remembered.state,
                "calendar_mode": remembered.mode,
                **status,
            },
        ))
        return

    if _emit_calendar_reconnect_if_stale(session, status):
        return

    local_oauth = bool(status.get("local_oauth_configured"))
    save_session(session)
    emit(_agent_out(
        session,
        awaiting_user="calendar_consent",
        prompt=(
            "Pick the calendar route from what you observe at runtime: `connector` when "
            "your platform can connect Google Calendar for you and hands the access token "
            "to commands in an environment variable you can name, or `local-oauth` when "
            "local_oauth_configured is true and you can relay a sign-in link to the user. "
            "If neither route is available, run "
            f"`{_CALENDAR_SKIP_SESSION_ONLY}` without asking. Otherwise ask the user which "
            "they prefer for Google Calendar: (1) only check free/busy slots for "
            "conflicts, (2) also add the booked service as a calendar event after "
            "confirmation, or (3) neither. Map (1) and (2) to "
            "`booking-agent flow calendar connect --mode freebusy|events` with "
            f"`--route {ROUTE_CONNECTOR} --token-env <NAME>` or `--route {ROUTE_LOCAL_OAUTH}`, "
            "and (3) to `booking-agent flow calendar skip`."
        ),
        next_command=None,
        data={
            "local_oauth_configured": local_oauth,
            "calendar_routes": list(CALENDAR_ROUTES),
            "calendar_choices": [
                {"choice": "freebusy", "command": "booking-agent flow calendar connect --mode freebusy --route <ROUTE>"},
                {"choice": "events", "command": "booking-agent flow calendar connect --mode events --route <ROUTE>"},
                {"choice": "neither", "command": "booking-agent flow calendar skip"},
            ],
            **status,
        },
    ))


@calendar_app.command("skip")
def calendar_skip(
    session_only: bool = typer.Option(
        False,
        "--session-only",
        help="Skip calendar checking for this booking only; keep the saved preference",
    ),
) -> None:
    session = must_load()
    try:
        require_business(session)
        require_checkout_prereq(session)
    except GateError as e:
        raise _handle_gate(session, e)
    _stop_pending_sign_in()
    if session_only:
        skip_calendar_gate_session_only(session)
        emit(_agent_out(
            session,
            prompt=(
                "Continuing this booking without calendar checking; the saved calendar "
                "preference is unchanged. Ready to query availability."
            ),
            next_command="booking-agent flow availability",
            data={"calendar_state": "skipped", "calendar_mode": CALENDAR_MODE_NONE},
        ))
        return
    complete_calendar_gate_with_preference(
        session,
        state="skipped",
        mode=CALENDAR_MODE_NONE,
    )
    emit(_agent_out(
        session,
        prompt="Calendar gate satisfied (user opted out). Ready to query availability.",
        next_command="booking-agent flow availability",
    ))


@calendar_app.command("connect")
def calendar_connect(
    mode: str = typer.Option(
        CALENDAR_MODE_FREEBUSY,
        "--mode",
        help="freebusy = conflict checking only; events = conflict checking + post-booking event",
    ),
    route: Optional[str] = typer.Option(
        None,
        "--route",
        help=(
            "connector = the agent's platform supplies a Google Calendar token in an "
            "environment variable; local-oauth = sign in through a link on this device"
        ),
    ),
    token_env: Optional[str] = typer.Option(
        None,
        "--token-env",
        help="Connector route: NAME of the environment variable holding the token (never the token)",
    ),
    timeout: int = typer.Option(
        CALENDAR_APPROVAL_TIMEOUT_SECONDS,
        "--timeout",
        min=30,
        help="Local OAuth: seconds the sign-in link stays valid.",
    ),
) -> None:
    session = must_load()
    try:
        require_business(session)
        require_checkout_prereq(session)
    except GateError as e:
        raise _handle_gate(session, e)

    if mode not in _VALID_CALENDAR_MODES:
        emit_error(
            session.flow_phase,
            code="invalid_calendar_mode",
            message=f"--mode must be one of: {', '.join(sorted(_VALID_CALENDAR_MODES))}",
            next_command="booking-agent flow calendar ask",
        )
        raise typer.Exit(code=1)

    if route not in CALENDAR_ROUTES:
        emit_error(
            session.flow_phase,
            code="calendar_route_required",
            message=(
                f"Pass --route {ROUTE_CONNECTOR} when your platform connects Google Calendar "
                "and exposes the access token in an environment variable (also pass "
                f"--token-env NAME), or --route {ROUTE_LOCAL_OAUTH} to relay a sign-in link. "
                "The CLI cannot tell which one this runtime supports."
            ),
            next_command=None,
            data={"calendar_routes": list(CALENDAR_ROUTES), "mode": mode},
        )
        raise typer.Exit(code=1)

    if route == ROUTE_CONNECTOR:
        _connect_via_connector(session, mode=mode, token_env=token_env)
        return
    _connect_via_local_oauth(session, mode=mode, timeout=timeout)


def _emit_calendar_connected(session: Session, *, mode: str, data: dict) -> None:
    prompt = (
        "Calendar connected for conflict checking and post-booking events."
        if mode == CALENDAR_MODE_EVENTS
        else "Calendar connected for conflict checking (free/busy only)."
    )
    emit(_agent_out(
        session,
        prompt=f"{prompt} Ready to query availability.",
        next_command="booking-agent flow availability",
        data={**data, "mode": mode},
    ))


def _stop_pending_sign_in() -> bool:
    cal = resolve_calendar_service()
    try:
        return bool(asyncio.run(cal.cleanup_auth()))
    except Exception:
        return False


def _connect_via_connector(session: Session, *, mode: str, token_env: Optional[str]) -> None:
    if not valid_token_env_name(token_env):
        emit_error(
            session.flow_phase,
            code="calendar_token_env_required",
            message=(
                f"--route {ROUTE_CONNECTOR} needs --token-env NAME: the environment variable "
                "your platform puts the Google Calendar access token in. Pass only the name."
            ),
            next_command=None,
            data={"mode": mode, "scope_request": scope_request(mode)},
        )
        raise typer.Exit(code=1)

    command = _connect_command(mode, ROUTE_CONNECTOR, token_env)
    request = scope_request(mode)
    token = host_token(token_env)
    scope, info = inspect_access_token(token) if token else (None, {})
    if not scope:
        out = {
            "error": "calendar_not_connected",
            "mode": mode,
            "token_env": token_env,
            "scope_request": request,
            "retry_command": command,
        }
        if token:
            out["connection_problem"] = {k: v for k, v in info.items() if k in ("error", "http_status")}
        save_session(session)
        emit(_agent_out(
            session,
            awaiting_user="calendar_connector",
            prompt=(
                f"Google Calendar is not connected to you, or {token_env} is empty in this "
                "command's environment. Connect Google Calendar through your platform's own "
                f"mechanism with this access: {request}. Then rerun `{command}`. If your "
                f"platform cannot connect it, run `{_CALENDAR_SKIP_SESSION_ONLY}`."
            ),
            next_command=command,
            data=out,
        ))
        return

    if not scope_satisfies_mode(scope, mode):
        emit_error(
            session.flow_phase,
            code="calendar_insufficient_scope",
            message=(
                f"The connected Google Calendar access does not cover '{mode}'. Ask your "
                f"platform to reconnect Google Calendar with this access: {request}. Then "
                f"rerun `{command}`."
            ),
            next_command=command,
            data={"scope": scope, "mode": mode, "scope_request": request, "token_env": token_env},
        )
        raise typer.Exit(code=1)

    cache_host_token(token, info, user_key=_CALENDAR_SESSION_ID)
    _stop_pending_sign_in()
    remember_route(ROUTE_CONNECTOR, token_env, session)
    _complete_calendar_gate(session, mode=mode)
    _emit_calendar_connected(
        session,
        mode=mode,
        data={"connected": True, "scope": scope, "calendar_route": ROUTE_CONNECTOR, "token_env": token_env},
    )


def _oauth_retry_choices(mode: str) -> list:
    return [
        {"choice": "new_link", "command": _connect_command(mode, ROUTE_LOCAL_OAUTH)},
        {"choice": "continue_without_calendar", "command": _CALENDAR_SKIP_SESSION_ONLY},
    ]


def _connect_via_local_oauth(session: Session, *, mode: str, timeout: int) -> None:
    from booking_agent.calendar.oauth_listener import CallbackPortBusy

    cal = resolve_calendar_service()
    cached = cal.load_cached_connection(_CALENDAR_SESSION_ID, _CALENDAR_SESSION_ID, mode)
    if cached:
        remember_route(ROUTE_LOCAL_OAUTH, None, session)
        _complete_calendar_gate(session, mode=mode)
        _emit_calendar_connected(session, mode=mode, data={**cached, "calendar_route": ROUTE_LOCAL_OAUTH})
        return

    if not local_oauth_configured():
        emit_error(
            session.flow_phase,
            code="calendar_oauth_not_configured",
            message=(
                "Local Google Calendar sign-in is not configured on this device. Use "
                f"--route {ROUTE_CONNECTOR} --token-env NAME if your platform can connect "
                f"Google Calendar, or run `{_CALENDAR_SKIP_SESSION_ONLY}`."
            ),
            next_command=_CALENDAR_SKIP_SESSION_ONLY,
            data={"mode": mode, "local_oauth_configured": False},
        )
        raise typer.Exit(code=1)

    async def _go():
        return await cal.initiate_connection(
            session_id=_CALENDAR_SESSION_ID,
            user_key=_CALENDAR_SESSION_ID,
            mode=mode,
            timeout=timeout,
        )

    try:
        result = asyncio.run(_go())
    except CallbackPortBusy as e:
        emit_error(
            session.flow_phase,
            code="calendar_callback_port_busy",
            message=(
                f"{e} No sign-in link was created. Another program is using that port; "
                f"run `{_CALENDAR_SKIP_SESSION_ONLY}` to continue without calendar checking."
            ),
            next_command=_CALENDAR_SKIP_SESSION_ONLY,
            data={"port": e.port, "mode": mode},
        )
        raise typer.Exit(code=1)
    except Exception as e:
        emit_error(session.flow_phase, code="calendar_connect_failed", message=str(e))
        raise typer.Exit(code=1)

    if result.get("connected"):
        remember_route(ROUTE_LOCAL_OAUTH, None, session)
        _complete_calendar_gate(session, mode=mode)
        _emit_calendar_connected(session, mode=mode, data={**result, "calendar_route": ROUTE_LOCAL_OAUTH})
        return

    auth_url = result.get("auth_url")
    scope_hint = (
        "free/busy visibility and permission to add events"
        if mode == CALENDAR_MODE_EVENTS
        else "free/busy visibility only"
    )
    valid_minutes = max(1, timeout // 60)
    save_session(session)
    emit(_agent_out(
        session,
        awaiting_user="oauth_approval",
        prompt=(
            f"Present the raw auth_url to the user (never markdown [label](url)) and ask them "
            f"to open it and approve Google Calendar access ({scope_hint}): {auth_url}  "
            f"Tell them the link works for about {valid_minutes} minutes. Then run "
            f"`{_CALENDAR_POLL}` without blocking your turn; it returns within its --wait "
            "budget, so run it again while the result stays pending."
        ),
        next_command=_CALENDAR_POLL,
        data={
            "auth_url": auth_url,
            "mode": mode,
            "link_valid_minutes": valid_minutes,
            "calendar_route": ROUTE_LOCAL_OAUTH,
        },
    ))


@calendar_app.command("poll")
def calendar_poll(
    wait: int = typer.Option(
        CALENDAR_POLL_WAIT_SECONDS,
        "--wait",
        min=0,
        help="Seconds to wait for the sign-in to finish before reporting it is still pending.",
    ),
) -> None:
    """Report the local OAuth sign-in started by `calendar connect --route local-oauth`."""
    session = must_load()
    try:
        require_business(session)
        require_checkout_prereq(session)
    except GateError as e:
        raise _handle_gate(session, e)

    cal = resolve_calendar_service()
    started = time.monotonic()
    while True:
        result = asyncio.run(cal.poll_auth(_CALENDAR_SESSION_ID))
        state = result.get("status")
        if state != "pending" or time.monotonic() - started >= wait:
            break
        time.sleep(1)

    mode = result.get("mode") if result.get("mode") in _VALID_CALENDAR_MODES else CALENDAR_MODE_FREEBUSY
    if state == "pending":
        emit(_agent_out(
            session,
            awaiting_user="oauth_approval",
            prompt=(
                "The user has not finished the Google Calendar sign-in yet. Do not post the "
                f"link again unless they ask. Run `{_CALENDAR_POLL}` again."
            ),
            next_command=_CALENDAR_POLL,
            data={"status": "pending", "mode": mode, "calendar_route": ROUTE_LOCAL_OAUTH},
        ))
        return

    if state == "connected":
        asyncio.run(cal.cleanup_auth())
        connected = cal.load_cached_connection(_CALENDAR_SESSION_ID, _CALENDAR_SESSION_ID, mode)
        if not connected:
            emit_error(
                session.flow_phase,
                code="calendar_insufficient_scope",
                message=(
                    f"The Google sign-in finished without the access '{mode}' needs "
                    f"({scope_request(mode)}). Ask whether the user wants a new link."
                ),
                next_command=None,
                data={"mode": mode, "scope_request": scope_request(mode), "choices": _oauth_retry_choices(mode)},
            )
            raise typer.Exit(code=1)
        remember_route(ROUTE_LOCAL_OAUTH, None, session)
        _complete_calendar_gate(session, mode=mode)
        _emit_calendar_connected(session, mode=mode, data={**connected, "calendar_route": ROUTE_LOCAL_OAUTH})
        return

    asyncio.run(cal.cleanup_auth())
    if state == "none":
        emit_error(
            session.flow_phase,
            code="calendar_oauth_not_started",
            message="No Google Calendar sign-in is pending. Run calendar ask to choose again.",
            next_command="booking-agent flow calendar ask",
        )
        raise typer.Exit(code=1)

    new_link = _connect_command(mode, ROUTE_LOCAL_OAUTH)
    if state == "timeout":
        code = "calendar_oauth_timeout"
        message = "The Google Calendar sign-in link expired before it was approved."
        user_message = (
            "The Google Calendar link expired before it was approved. Would you like a "
            "new link, or should I continue this booking without checking your calendar?"
        )
    else:
        code = "calendar_oauth_failed"
        message = result.get("error") or "The Google Calendar sign-in did not finish."
        user_message = (
            "Google Calendar did not connect. Would you like a new link, or should I "
            "continue this booking without checking your calendar?"
        )
    emit_error(
        session.flow_phase,
        code=code,
        message=(
            f"{message} Do not start a new link on your own. Post user_message and STOP. Run "
            f"`{new_link}` only if the user asks for a new link, or "
            f"`{_CALENDAR_SKIP_SESSION_ONLY}` if they want to continue without calendar checking."
        ),
        user_message=user_message,
        next_command=None,
        data={"choices": _oauth_retry_choices(mode)},
    )
    raise typer.Exit(code=1)


@calendar_app.command("cleanup")
def calendar_cleanup() -> None:
    """Stop a pending local OAuth sign-in and release its callback port."""
    session = must_load()
    stopped = _stop_pending_sign_in()
    emit(_agent_out(
        session,
        prompt=(
            "Stopped the pending Google Calendar sign-in." if stopped
            else "No Google Calendar sign-in was running."
        ),
        next_command="booking-agent flow calendar ask",
        data={"stopped": stopped},
    ))


@calendar_app.command("create-event")
def calendar_create_event() -> None:
    """Create a Google Calendar event after booking is CONFIRMED."""
    session = must_load()

    if session.calendar_state == "skipped" or session.calendar_mode == CALENDAR_MODE_NONE:
        emit(_agent_out(
            session,
            prompt="Calendar was skipped at the gate; no event created.",
            data={"skipped": True},
        ))
        return

    if session.calendar_mode == CALENDAR_MODE_FREEBUSY:
        emit(_agent_out(
            session,
            prompt="User chose free/busy only; no calendar event created.",
            data={"skipped": True, "reason": "freebusy_only"},
        ))
        return

    if session.flow_phase != "CONFIRMED":
        emit_error(
            session.flow_phase,
            code="wrong_phase",
            message=(
                f"Cannot create calendar event in phase '{session.flow_phase}'. "
                "Wait until phase is CONFIRMED."
            ),
            next_command="booking-agent flow validate-skill-compliance",
        )
        raise typer.Exit(code=1)

    result = create_calendar_event(session, user_key=_CALENDAR_SESSION_ID)
    if not result.get("ok"):
        emit_error(
            session.flow_phase,
            code=result.get("code", "calendar_write_failed"),
            message=result.get("message", "Failed to create calendar event."),
        )
        raise typer.Exit(code=1)

    html_link = result.get("html_link")
    prompt = "Calendar event created for this booking."
    if html_link:
        prompt += (
            f" Show the user the raw html_link URL (never markdown [label](url)): {html_link}"
        )
    emit(_agent_out(
        session,
        prompt=prompt,
        data={
            "event_id": result.get("event_id"),
            "html_link": html_link,
            "idempotent": result.get("idempotent", False),
        },
    ))


@calendar_app.command("update-event")
def calendar_update_event() -> None:
    """Move this booking's Google Calendar event to its current time after a reschedule."""
    session = must_load()

    if session.calendar_state == "skipped" or session.calendar_mode in (
        CALENDAR_MODE_NONE, CALENDAR_MODE_FREEBUSY,
    ):
        emit(_agent_out(
            session,
            prompt="No calendar event is kept for this booking; nothing to update.",
            data={"skipped": True},
        ))
        return

    if session.flow_phase != "CONFIRMED":
        emit_error(
            session.flow_phase,
            code="wrong_phase",
            message=(
                f"Cannot update the calendar event in phase '{session.flow_phase}'. "
                "Only a confirmed booking has an event to move."
            ),
        )
        raise typer.Exit(code=1)

    result = update_calendar_event(session, user_key=_CALENDAR_SESSION_ID)
    if not result.get("ok"):
        emit_error(
            session.flow_phase,
            code=result.get("code", "calendar_write_failed"),
            message=(
                "The appointment itself is booked at its current time, but the calendar "
                "event could not be updated: "
                + result.get("message", "unknown error")
            ),
        )
        raise typer.Exit(code=1)

    html_link = result.get("html_link")
    prompt = (
        "Calendar event created for the new time."
        if result.get("created")
        else "Calendar event moved to the new time."
    )
    if html_link:
        prompt += (
            f" Show the user the raw html_link URL (never markdown [label](url)): {html_link}"
        )
    emit(_agent_out(
        session,
        prompt=prompt,
        data={
            "event_id": result.get("event_id"),
            "html_link": html_link,
            "created": bool(result.get("created")),
        },
    ))


@calendar_app.command("delete-event")
def calendar_delete_event() -> None:
    """Remove a cancelled booking's Google Calendar event (retry after a failed cleanup)."""
    session = must_load()
    if session.booking.status != "canceled":
        emit_error(
            session.flow_phase,
            code="booking_not_canceled",
            message=(
                "Only a cancelled booking's calendar event is removed. Cancel the booking "
                "first with `booking-agent flow book cancel`."
            ),
        )
        raise typer.Exit(code=1)
    result = delete_calendar_event(session, user_key=_CALENDAR_SESSION_ID)
    if not result.get("ok"):
        emit_error(
            session.flow_phase,
            code=result.get("code", "calendar_delete_failed"),
            message=(
                "The booking is cancelled, but its calendar event could not be removed. "
                + (result.get("message") or "")
            ).strip(),
            data={"html_link": result.get("html_link")},
        )
        raise typer.Exit(code=1)
    prompt = {
        "deleted": "Calendar event removed for the cancelled booking.",
        "already_missing": "The calendar event was already gone.",
    }.get(result.get("status"), "No calendar event is kept for this booking; nothing to remove.")
    emit(_agent_out(session, prompt=prompt, data={"calendar": result}))


# ---------------------------------------------------------------------------
# availability / slots
# ---------------------------------------------------------------------------


_DATE_ONLY_WINDOW_DAYS = 7

_NO_BUSINESS_ZONE_TEXT = (
    "Tell the user the business did not name its timezone, so each time is "
    "shown with its UTC offset as the business sent it."
)


def _window_zone(tz_name: Optional[str]):
    valid = _valid_iana_timezone(tz_name)
    return ZoneInfo(valid) if valid else timezone.utc


def _default_window(tz_name: Optional[str] = None):
    """Tomorrow 00:00:00 through +14 days 23:59:59 in the business zone (UTC if unknown)."""
    now = datetime.now(_window_zone(tz_name))
    start = now + timedelta(days=1)
    end = now + timedelta(days=14)
    return start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(), end.replace(
        hour=23, minute=59, second=59, microsecond=0
    ).isoformat()


def _day_window(target_date: str, tz_name: Optional[str] = None):
    """Full-day window (00:00:00 to 23:59:59) for an ISO date `YYYY-MM-DD` in the business zone."""
    day = datetime.fromisoformat(target_date)
    if day.tzinfo is None:
        day = day.replace(tzinfo=_window_zone(tz_name))
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=0)
    return start.isoformat(), end.isoformat()


def _resolve_window(
    start: Optional[str], end: Optional[str], target_date: Optional[str],
    tz_name: Optional[str] = None,
) -> tuple:
    """``(from, to)`` for an availability query. Raises ValueError on a bad date.

    Without a known business zone the bounds are plain dates, so the business
    reads them on its own calendar (USP 4.3.1), within the 7-day guidance.
    """
    if _valid_iana_timezone(tz_name) is None:
        if target_date and not (start or end):
            day = datetime.fromisoformat(target_date).date().isoformat()
            return day, day
        tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
        auto_start = tomorrow.isoformat()
        auto_end = (tomorrow + timedelta(days=_DATE_ONLY_WINDOW_DAYS - 1)).isoformat()
        return start or auto_start, end or auto_end
    if target_date and not (start or end):
        auto_start, auto_end = _day_window(target_date, tz_name)
    else:
        auto_start, auto_end = _default_window(tz_name)
    return start or auto_start, end or auto_end


CONFIRMED_ADDRESS_TIMEZONE_SOURCE = "confirmed_address"


def _confirmed_address_coordinates(session: Session) -> Optional[tuple]:
    """Coordinates of the address the user confirmed: service address first, then search area."""
    coords = (session.service_address or {}).get("coordinates") or {}
    if coords.get("lat") is not None and coords.get("lng") is not None:
        return float(coords["lat"]), float(coords["lng"])
    area = session.search_area or {}
    if area.get("confirmed") and area.get("lat") is not None and area.get("lng") is not None:
        return float(area["lat"]), float(area["lng"])
    return None


def _ensure_buyer_timezone(session: Session, *, link_cli_bin: Optional[str] = None) -> None:
    """Resolve the buyer zone from the confirmed address, else the default Link shipping address.

    Never falls back to the device, process, VPN, or host timezone.
    """
    if _valid_iana_timezone(session.buyer_timezone):
        return
    coords = _confirmed_address_coordinates(session)
    if coords is not None:
        try:
            session.buyer_timezone = tz_context.timezone_for_coordinates(*coords)
            session.buyer_timezone_source = CONFIRMED_ADDRESS_TIMEZONE_SOURCE
            session.buyer_timezone_error = None
            return
        except tz_context.BuyerTimezoneError:
            pass
    from ..link_cli import retrieve_default_shipping_address

    result = tz_context.lookup_buyer_timezone(
        lambda: retrieve_default_shipping_address(link_cli_bin=link_cli_bin)
    )
    session.buyer_timezone = result.timezone
    session.buyer_timezone_source = result.source
    session.buyer_timezone_error = result.error


def _compact_slot_displays(session: Session, slot: SlotRef, *, dual: bool) -> Dict[str, Any]:
    """Short start-to-end displays: business zone, buyer zone, and Google Calendar zone."""
    from ..booking_summary import calendar_differs

    biz = session.business.timezone
    out: Dict[str, Any] = {}
    short = tz_context.compact_range_display(slot.start, slot.end, biz)
    if short:
        out["business_range_display"] = short
    if dual:
        buyer = tz_context.compact_clock_range(
            slot.start, slot.end, session.buyer_timezone, reference_tz=biz,
        )
        if buyer:
            out["buyer_range_display"] = buyer
    if short and calendar_differs(session, [slot.start]):
        cal = tz_context.compact_clock_range(
            slot.start, slot.end, session.calendar_timezone, reference_tz=biz,
        )
        if cal:
            out["calendar_display"] = cal
    return out


def _find_presented_slot(session: Session, slot_id: Optional[str]) -> Optional[SlotRef]:
    return next((s for s in session.slots_presented if s.slot_id == slot_id), None)


def _localized_schedules(
    session: Session, schedules: Optional[List[Dict[str, Any]]], currency: Optional[str],
) -> List[Dict[str, Any]]:
    """Payment schedules with display amounts and the due time in business (and buyer) time."""
    dual = tz_context.zones_differ(
        session.business.timezone, session.buyer_timezone,
        [s.get("due_at") for s in schedules or [] if isinstance(s, dict)],
    )
    out = []
    for item in schedules or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if row.get("amount") is not None and currency:
            row["amount_display"] = _format_price_display(int(row["amount"]), currency)
        due_display = tz_context.format_instant(row.get("due_at"), session.business.timezone)
        if due_display:
            row["due_at_display"] = due_display
            if dual:
                row["due_at_buyer_display"] = tz_context.format_instant(
                    row["due_at"], session.buyer_timezone,
                )
        out.append(row)
    return out


def _localized_messages(
    session: Session, messages: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    out = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        if isinstance(row.get("content"), str):
            row["content_display"] = tz_context.localize_instants_in_text(
                row["content"], session.business.timezone,
            )
        out.append(row)
    return out


def _group_slots_by_time(slots: List[Any]) -> List[SlotRef]:
    """One user-facing slot per (start, end); other resources' ids become alternates.

    The first slot the business returned for a time stays the primary, so selection
    is deterministic and the agent never sees the same time twice.
    """
    grouped: Dict[tuple, SlotRef] = {}
    for s in slots:
        key = (s.start, s.end)
        existing = grouped.get(key)
        if existing is not None:
            existing.alternate_slot_ids.append(s.id)
            continue
        grouped[key] = SlotRef(
            slot_id=s.id,
            start=s.start,
            end=s.end,
            duration=s.duration,
            price_amount=(s.pricing.amount if s.pricing and s.pricing.amount is not None else None),
        )
    return list(grouped.values())


def _service_coordinates(session: Session) -> Optional[tuple]:
    from ..catalog import channel_type
    from ..geo_eligibility import _coordinates_from_location

    if channel_type(session.live_service) == "at_buyer_location":
        coords = (session.service_address or {}).get("coordinates") or {}
        if coords.get("lat") is not None and coords.get("lng") is not None:
            return float(coords["lat"]), float(coords["lng"])

    for loc in (session.live_service or {}).get("locations") or []:
        coords = _coordinates_from_location(loc)
        if coords is not None:
            return coords
    row = _discover_row(session, session.service_id) or {}
    result = row.get("registry_search_result")
    if isinstance(result, dict):
        return _coordinates_from_location(result.get("location"))
    return None


def _timezone_gate(session: Session) -> Dict[str, Any]:
    """What the user must explicitly accept about times before a slot can be selected."""
    gate: Dict[str, Any] = {}
    conflict = tz_context.service_location_timezone_conflict(
        session.business.timezone,
        _service_coordinates(session),
        [s.start for s in session.slots_presented],
    )
    if conflict is not None:
        gate["timezone_conflict"] = conflict
    channel = ((session.live_service or {}).get("channel") or {}).get("type")
    if channel == "at_buyer_location" and not _valid_iana_timezone(session.buyer_timezone):
        gate["buyer_timezone_unresolved_for_home_visit"] = session.buyer_timezone_error or "unknown"
    return gate


def _selected_slot_zones_differ(session: Session) -> bool:
    slot = _find_presented_slot(session, session.slot_id)
    return tz_context.zones_differ(
        session.business.timezone, session.buyer_timezone, [slot.start] if slot else [],
    )


async def _query_slots_async(
    session: Session, service_id: str, window_from: str, window_to: str,
):
    """Live service summary and open slots, minus connected-calendar conflicts.

    Returns ``(live_service_summary, slots, calendar_filtered, slots_filtered,
    response_zones)`` where ``response_zones`` holds the response's
    ``business_timezone`` and ``applied_date_bounds_timezone``.
    """
    from booking_agent.calendar.filter import filter_slots_by_busy_times
    from booking_agent.headless import query_availability_with_quirk
    from booking_agent.step_timing import timed_step

    from ..catalog import live_service_summary

    backend = resolve_backend()
    async with timed_step("availability"):
        profile = await backend.resolve_profile(session.business.profile_url)
        live = await backend.get_service(profile, service_id)
        if live.id != service_id:
            raise RuntimeError(
                f"Live catalog id {live.id!r} does not match requested service_id {service_id!r}"
            )
        slots = await query_availability_with_quirk(
            backend, profile, service_id, window_from, window_to,
            timezone_str=_valid_iana_timezone(session.business.timezone),
        )
        response_zones = {
            "business_timezone": getattr(slots, "business_timezone", None),
            "applied_date_bounds_timezone": getattr(slots, "applied_date_bounds_timezone", None),
        }
        calendar_filtered = False
        slots_before_filter = len(slots)
        slots_filtered = 0

        if session.calendar_state == "connected":
            cal = resolve_calendar_service()
            mode = (
                session.calendar_mode
                if session.calendar_mode in _VALID_CALENDAR_MODES
                else CALENDAR_MODE_FREEBUSY
            )
            refresh_host_token(session, mode)
            # Each CLI invocation is a new process, so load the saved token into memory.
            cal.load_cached_connection(_CALENDAR_SESSION_ID, _CALENDAR_SESSION_ID, mode)
            if not await cal.is_connected(_CALENDAR_SESSION_ID):
                raise RuntimeError(
                    "Calendar was connected but is no longer available. Run "
                    f"`{_connect_command(mode, *(_session_route(session)))}` "
                    f"again or `{_CALENDAR_SKIP_SESSION_ONLY}`."
                )
            busy_blocks = await cal.get_busy_times(
                _CALENDAR_SESSION_ID, *_calendar_window(window_from, window_to),
            )
            slots = filter_slots_by_busy_times(slots, busy_blocks)
            calendar_filtered = True
            if mode == CALENDAR_MODE_EVENTS:
                try:
                    session.calendar_timezone = _valid_iana_timezone(
                        await cal.get_calendar_timezone(_CALENDAR_SESSION_ID),
                    )
                except Exception:
                    session.calendar_timezone = None
            slots_filtered = slots_before_filter - len(slots)

    return live_service_summary(live), slots, calendar_filtered, slots_filtered, response_zones


def _calendar_window(window_from: str, window_to: str) -> tuple[str, str]:
    """Exact UTC bounds for a busy-time lookup.

    A plain date has no offset, so it is widened to every zone that day can
    fall in (UTC+14 through UTC-12).
    """
    def bound(value: str, *, end: bool) -> str:
        if "T" in value:
            return value
        day = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        if end:
            return (day + timedelta(days=1, hours=12)).isoformat()
        return (day - timedelta(hours=14)).isoformat()

    return bound(window_from, end=False), bound(window_to, end=True)


def _adopt_response_timezone(session: Session, response_zones: Dict[str, Any]) -> Dict[str, Any]:
    """Store a valid response ``business_timezone`` on the session (USP 4.3.1).

    Returns command data fields. A missing or invalid value never replaces a
    valid stored zone and never fails the command.
    """
    stored = _valid_iana_timezone(session.business.timezone)
    reported = _valid_iana_timezone(response_zones.get("business_timezone"))
    out: Dict[str, Any] = {
        "applied_date_bounds_timezone": response_zones.get("applied_date_bounds_timezone"),
    }
    if reported is None or reported == stored:
        return out
    if stored is not None:
        out["timezone_discrepancy"] = {
            "stored": stored, "availability_response": reported,
        }
    session.business.timezone = reported
    out["business_timezone_source"] = "availability_response"
    return out


@app.command("availability")
def availability(
    service_id: Optional[str] = typer.Option(None, "--service-id"),
    start: Optional[str] = typer.Option(None, "--from", help="ISO8601 start (default: tomorrow)"),
    end: Optional[str] = typer.Option(None, "--to", help="ISO8601 end (default: +14 days)"),
    target_date: Optional[str] = typer.Option(
        None,
        "--target-date",
        help="Single calendar day to query (YYYY-MM-DD). Sets a full-day window when "
        "--from/--to are not given. Use this to honor a specific requested date.",
    ),
) -> None:
    """Query availability for the selected business + service."""
    session = must_load()
    try:
        require_business(session)
        require_calendar_gate(session)
    except GateError as e:
        raise _handle_gate(session, e)

    if session.business.timezone and _valid_iana_timezone(session.business.timezone) is None:
        emit_error(
            session.flow_phase,
            code="business_timezone_invalid",
            message=(
                "The selected business has a timezone on record that is not a valid "
                "IANA zone, so availability cannot be shown in its local time. Re-run "
                "`booking-agent flow business use` for this business."
            ),
            next_command="booking-agent flow business use --profile-url <url> --service-id <id>",
        )
        raise typer.Exit(code=1)

    if not service_id:
        emit_error(
            session.flow_phase,
            code="missing_service_id",
            message="--service-id is required (use the value from `flow discover`).",
        )
        raise typer.Exit(code=1)

    try:
        window_from, window_to = _resolve_window(
            start, end, target_date, session.business.timezone,
        )
    except ValueError:
        emit_error(
            session.flow_phase,
            code="invalid_target_date",
            message=f"--target-date must be an ISO date (YYYY-MM-DD); got {target_date!r}.",
        )
        raise typer.Exit(code=1)

    try:
        live_service, slots, calendar_filtered, slots_filtered, response_zones = asyncio.run(
            _query_slots_async(session, service_id, window_from, window_to)
        )
    except Exception as e:
        emit_error(session.flow_phase, code="availability_failed", message=str(e))
        raise typer.Exit(code=1)

    from ..flow_timings import apply_pending_timings

    apply_pending_timings(session)
    zone_data = _adopt_response_timezone(session, response_zones)

    session.availability_window = AvailabilityWindow(from_=window_from, to=window_to)
    session.slots_presented = _group_slots_by_time(slots)
    session.service_id = service_id
    session.live_service = live_service
    session.slots_presented_dual_zone = False
    session.timezone_difference_shown = False
    session.timezone_conflict_confirmed = False
    _ensure_buyer_timezone(session)
    transition(session, to="AVAILABILITY_QUERIED")
    save_session(session)

    filter_note = ""
    if calendar_filtered:
        if slots_filtered:
            filter_note = (
                f" Removed {slots_filtered} slot(s) that conflict with the "
                "connected calendar."
            )
        else:
            filter_note = " No calendar conflicts found in this window."

    emit(_agent_out(
        session,
        prompt=(
            f"{len(session.slots_presented)} slot(s) available for service {service_id} "
            f"between {window_from} and {window_to}.{filter_note} Run "
            "`booking-agent flow slots list` to present them, then "
            "`booking-agent flow slots select --slot-id <id>` ONCE THE USER PICKS."
        ),
        next_command="booking-agent flow slots list",
        data={
            "service_id": service_id,
            "live_service": live_service,
            "slot_count": len(session.slots_presented),
            "resource_slot_count": len(slots),
            "calendar_filtered": calendar_filtered,
            "slots_filtered": slots_filtered,
            "business_timezone": session.business.timezone,
            **zone_data,
            "buyer_timezone": session.buyer_timezone,
            "buyer_timezone_unresolved": session.buyer_timezone_error,
        },
    ))


slots_app = typer.Typer(help="Slot listing and user-confirmed selection.")
app.add_typer(slots_app, name="slots")


@slots_app.command("list")
def slots_list() -> None:
    session = must_load()
    try:
        require_phase(session, "AVAILABILITY_QUERIED", "SLOTS_PRESENTED", "SLOT_SELECTED",
                      code="no_availability_queried",
                      next_command="booking-agent flow availability")
    except GateError as e:
        raise _handle_gate(session, e)
    business_tz = session.business.timezone
    buyer_tz = session.buyer_timezone
    dual = tz_context.zones_differ(
        business_tz, buyer_tz, [s.start for s in session.slots_presented],
    )
    session.slots_presented_dual_zone = dual
    transition(session, to="SLOTS_PRESENTED")
    save_session(session)
    data: Dict[str, Any] = {
        "slots": [
            {
                "slot_id": s.slot_id,
                "start": s.start,
                "end": s.end,
                **tz_context.slot_displays(s.start, s.end, business_tz, buyer_tz, dual=dual),
                **_compact_slot_displays(session, s, dual=dual),
                **({"resource_options": 1 + len(s.alternate_slot_ids)} if s.alternate_slot_ids else {}),
            }
            for s in session.slots_presented
        ],
        "business_timezone": business_tz,
    }
    prompt = (
        "Present these slots to the user as a numbered or bulleted list (never a "
        "markdown table with | pipes) and WAIT for them to pick. "
    )
    if dual:
        data["buyer_timezone"] = buyer_tz
        data["timezone_notice"] = tz_context.timezone_notice(business_tz, buyer_tz)
        prompt += (
            "Post data.timezone_notice once, above the list. Show each slot as its "
            "business_range_display first, with its buyer_range_display in parentheses "
            "(the full business_start_display and buyer_start_display are there if needed). "
        )
    elif _valid_iana_timezone(business_tz) is None:
        data["business_timezone_unknown"] = True
        prompt += f"Show each slot's business_start_display. {_NO_BUSINESS_ZONE_TEXT} "
    else:
        prompt += (
            "Show each slot's business_range_display (the business's local time, start "
            "to end). "
        )
        if session.buyer_timezone_error:
            data["buyer_timezone_unresolved"] = session.buyer_timezone_error
            data["timezone_fallback_note"] = (
                "Times are the business's local time, "
                f"{tz_context.zone_label(business_tz)}."
            )
            prompt += "Add data.timezone_fallback_note as one line under the list. "
    if any("calendar_display" in slot for slot in data["slots"]):
        prompt += (
            "The user's Google Calendar uses another timezone, so add each slot's "
            "calendar_display in parentheses as \"(<calendar_display> on your Google "
            "Calendar)\". "
        )
    select_cmd = "booking-agent flow slots select --slot-id <id> --user-confirmed"
    gate = _timezone_gate(session)
    if gate:
        data.update(gate)
        select_cmd += " --timezone-confirmed"
        if "timezone_conflict" in gate:
            prompt += (
                "The business's published timezone does not match where this service is "
                "located (data.timezone_conflict), so the listed times may be wrong. Tell "
                "the user plainly, above the list, that the times are shown in "
                f"{tz_context.zone_label(business_tz)} as the business published it, and "
                "that this looks inconsistent with the service location. "
            )
        if "buyer_timezone_unresolved_for_home_visit" in gate:
            prompt += (
                "This is a visit to the user's address and their local time could not be "
                "determined, so only the business's time is shown. "
            )
        prompt += (
            "When the user picks a slot, ask them to confirm that exact time in those "
            "words and STOP; pass --timezone-confirmed only after they confirm. "
        )
    prompt += f"Then call `{select_cmd}`."
    emit(_agent_out(
        session,
        awaiting_user="slot_choice",
        prompt=prompt,
        next_command=select_cmd,
        data=data,
    ))


@slots_app.command("select")
def slots_select(
    slot_id: str = typer.Option(..., "--slot-id"),
    user_confirmed: bool = typer.Option(
        False, "--user-confirmed",
        help="Required. Indicates the human, not the agent, picked this slot.",
    ),
    timezone_confirmed: bool = typer.Option(
        False, "--timezone-confirmed",
        help="The user confirmed the slot time despite a timezone conflict or unknown local time.",
    ),
) -> None:
    session = must_load()
    try:
        require_phase(
            session, "AVAILABILITY_QUERIED", "SLOTS_PRESENTED",
            code="no_slots_presented",
            next_command="booking-agent flow slots list",
        )
    except GateError as e:
        raise _handle_gate(session, e)

    if not user_confirmed:
        emit_error(
            session.flow_phase,
            code="slot_not_user_confirmed",
            message="Pass --user-confirmed only after the user picks a slot. "
                    "Auto-picking on the user's behalf is forbidden.",
            next_command="booking-agent flow slots list",
        )
        raise typer.Exit(code=1)

    chosen = next(
        (
            s for s in session.slots_presented
            if s.slot_id == slot_id or slot_id in s.alternate_slot_ids
        ),
        None,
    )
    if chosen is None:
        emit_error(
            session.flow_phase,
            code="unknown_slot_id",
            message=f"Slot '{slot_id}' is not in slots_presented. Run availability again.",
            next_command="booking-agent flow availability",
        )
        raise typer.Exit(code=1)

    gate = _timezone_gate(session)
    if gate and not timezone_confirmed:
        emit_error(
            session.flow_phase,
            code="timezone_confirmation_required",
            message=(
                "The slot time cannot be confirmed in the user's local time (see data). "
                "Ask the user to confirm the exact time as shown in the business's "
                "timezone, then re-run with --timezone-confirmed."
            ),
            next_command=(
                f"booking-agent flow slots select --slot-id {shlex.quote(chosen.slot_id)} "
                "--user-confirmed --timezone-confirmed"
            ),
            data=gate,
        )
        raise typer.Exit(code=1)

    session.timezone_conflict_confirmed = bool(gate)
    session.slot_id = chosen.slot_id
    session.slot_user_confirmed = True
    session.timezone_difference_shown = (
        session.flow_phase == "SLOTS_PRESENTED" and session.slots_presented_dual_zone
    )
    transition(session, to="SLOT_SELECTED")
    save_session(session)
    emit(_agent_out(
        session,
        prompt="Slot recorded. Next: authenticate Link wallet, then resolve buyer.",
        next_command="booking-agent flow link-auth connect",
    ))


# ---------------------------------------------------------------------------
# buyer / book / pay
# ---------------------------------------------------------------------------


buyer_app = typer.Typer(help="Buyer identity resolution via link-cli.")
app.add_typer(buyer_app, name="buyer")


def _selected_slot_view(session: Session, *, dual: bool) -> Optional[Dict[str, Any]]:
    slot = _find_presented_slot(session, session.slot_id)
    if slot is None:
        return None
    view: Dict[str, Any] = {
        "slot_id": slot.slot_id,
        "start": slot.start,
        "end": slot.end,
        "duration": slot.duration,
        "business_display": tz_context.range_display(
            slot.start, slot.end, session.business.timezone,
        ),
        "business_display_short": tz_context.compact_range_display(
            slot.start, slot.end, session.business.timezone,
        ),
        "business_timezone": session.business.timezone,
    }
    if dual:
        view["buyer_display"] = tz_context.range_display(
            slot.start, slot.end, session.buyer_timezone,
        )
        view["buyer_timezone"] = session.buyer_timezone
    return view


def _buyer_recap(
    session: Session, price_info: Dict[str, Any], *, dual: bool, notice_needed: bool,
) -> tuple[str, Dict[str, Any]]:
    """Single booking question for ``buyer resolve``, worded for the service's payment route."""
    route = session.payment_route or "at_booking"
    price = price_info.get("price_display")
    consistency = price_info.get("pricing_consistency") or {}
    if route == "free":
        summary = {"route": route, "charge_now_display": None, "due_at_appointment_display": None}
        payment_text = (
            "Say plainly that this service is free. Ask a single combined confirmation "
            "that only asks whether to book it. Do not bring up money."
        )
    elif route == "at_service":
        summary = {"route": route, "charge_now_display": None, "due_at_appointment_display": price}
        payment_text = (
            "State that data.price_display is paid to the business at the appointment and "
            "nothing is collected now. Ask a single combined confirmation that only asks "
            "whether to book it."
        )
    elif route == "deposit_required":
        summary = {"route": route, "charge_now_display": None, "total_display": price}
        payment_text = (
            "State that this service needs a deposit at booking, with the rest of "
            "data.price_display due later. The exact deposit due now and its terms come "
            "from the business at checkout and are shown before anything is charged. Ask a "
            "single combined confirmation to book it and charge only the deposit due now "
            "to their Link wallet."
        )
    else:
        summary = {"route": route, "charge_now_display": price, "due_at_appointment_display": None}
        payment_text = (
            "Include data.price_display (never raw amount_minor_units). Ask a single "
            "combined confirmation to book the appointment and charge that total to their "
            "Link wallet."
        )
    if consistency.get("mismatch"):
        payment_text = (
            f"Registry search showed {consistency.get('registry_price_display')!r} but the "
            f"live catalog says {consistency.get('live_catalog_price_display')!r}; quote "
            "data.price_display (live catalog), never the registry discover price. "
            + payment_text
        )
    time_text = "The When line is the business's local time."
    if _valid_iana_timezone(session.business.timezone) is None:
        time_text = _NO_BUSINESS_ZONE_TEXT
    elif dual:
        time_text = (
            "The When line gives the business's local time with the user's own time "
            "(data.selected_slot.buyer_display in parentheses), so one yes also covers the "
            "timezone difference."
        )
        if notice_needed:
            time_text = "Open with data.timezone_notice. " + time_text
    prompt = (
        "Buyer resolved from link-cli. **STOP.** In one message, show every line of "
        "data.booking_recap_lines verbatim, one per line, in order. Do not reword, merge, "
        f"or drop any line; it includes the exact address and the note for the business. "
        f"{time_text} Then ask one question. {payment_text} Do not run `flow book` until "
        "they approve. If they correct the note, re-run `booking-agent flow buyer resolve "
        "--notes '<corrected note>'`. If they correct the address, run `booking-agent flow "
        "search-area propose --purpose service-address`."
    )
    return prompt, summary


BOOKING_NOTES_MAX_CHARS = 500


@buyer_app.command("resolve")
def buyer_resolve(
    link_cli_bin: Optional[str] = typer.Option(
        None, "--link-cli-bin",
        help="Override path to the link-cli binary (default: `link-cli` on PATH).",
    ),
    notes: Optional[str] = typer.Option(
        None, "--notes",
        help=(
            "Short note for the business drawn from the user's request (the problem, "
            "the item, what they need). Never put the address, name, or contact details here."
        ),
    ),
) -> None:
    """Run `link-cli user-info retrieve` and store the Buyer in session."""
    session = must_load()
    try:
        require_phase(session, "SLOT_SELECTED", code="no_slot_selected",
                      next_command="booking-agent flow slots select --slot-id <id> --user-confirmed")
        require_link_auth_gate(session)
    except GateError as e:
        raise _handle_gate(session, e)

    from ..link_cli import (
        LinkCliUserInfoError,
        buyer_fields_from_user_info,
        retrieve_user_info,
    )

    try:
        payload = retrieve_user_info(link_cli_bin=link_cli_bin)
    except LinkCliUserInfoError as e:
        emit_error(
            session.flow_phase,
            code=e.code,
            message=e.message,
            next_command=(
                "booking-agent flow link-auth connect"
                if e.code == "link_cli_failed"
                else "booking-agent flow buyer resolve"
            ),
        )
        raise typer.Exit(code=1)

    name, email, phone = buyer_fields_from_user_info(payload)

    if not email or email.lower().endswith("@example.com"):
        emit_error(
            session.flow_phase,
            code="buyer_email_invalid",
            message=f"Buyer email '{email}' is missing or a placeholder. "
                    "Have the user complete Link authentication first.",
            next_command="booking-agent flow buyer resolve",
        )
        raise typer.Exit(code=1)

    session.buyer = Buyer(name=name, email=email, phone=phone)
    session.buyer_source = BUYER_SOURCE_LINKCLI
    if notes is not None:
        session.booking_notes = " ".join(notes.split())[:BOOKING_NOTES_MAX_CHARS] or None
    _ensure_buyer_timezone(session, link_cli_bin=link_cli_bin)
    dual = _selected_slot_zones_differ(session)
    notice_needed = dual and not session.timezone_difference_shown
    if notice_needed:
        session.timezone_difference_shown = True
    transition(session, to="BUYER_RESOLVED")
    save_session(session)

    from ..booking_summary import booking_recap_lines
    from ..catalog import payment_price_for_session

    price_info = payment_price_for_session(session)
    service_name = None
    live = session.live_service
    if isinstance(live, dict):
        service_name = live.get("name")
    consistency = price_info.get("pricing_consistency") or {}
    buyer_prompt, payment_summary = _buyer_recap(
        session, price_info, dual=dual, notice_needed=notice_needed,
    )
    data: Dict[str, Any] = {
        "buyer": {"name": name, "email": email, "phone": phone},
        "business_name": session.business.name,
        "service_name": service_name,
        "service_id": session.service_id,
        "selected_slot": _selected_slot_view(session, dual=dual),
        "price_display": price_info["price_display"],
        "currency": price_info.get("currency"),
        "amount_minor_units": price_info.get("amount_minor_units"),
        "price_source": price_info.get("price_source"),
        "pricing_consistency": consistency,
        "payment_route": session.payment_route,
        "payment_summary": payment_summary,
        "booking_recap_lines": booking_recap_lines(session, price_info, dual=dual),
        "notes_for_business": session.booking_notes,
        "service_address": session.service_address_label,
    }
    if notice_needed:
        data["timezone_notice"] = tz_context.timezone_notice(
            session.business.timezone, session.buyer_timezone,
        )
    if session.buyer_timezone_error and not dual:
        data["buyer_timezone_unresolved"] = session.buyer_timezone_error

    emit(_agent_out(
        session,
        prompt=buyer_prompt,
        next_command="booking-agent flow book",
        data=data,
    ))


book_app = typer.Typer(help="Create a booking and poll for its status.", invoke_without_command=True)
app.add_typer(book_app, name="book")


def _confirmed_delivery_address(
    session: Session, live_service: Optional[Dict[str, Any]], *, resume_command: str,
) -> Optional[Dict[str, Any]]:
    """The confirmed service address for a home visit, or None when the visit is elsewhere."""
    from ..catalog import channel_type

    if channel_type(live_service) != "at_buyer_location":
        return None
    if not session.service_address:
        session.area_resume_command = resume_command
        save_session(session)
        emit_error(
            session.flow_phase,
            code="service_address_unconfirmed",
            message=(
                "This service comes to the customer and no street address has been "
                "confirmed with the user. Propose their address and ask them first."
            ),
            next_command="booking-agent flow search-area propose --purpose service-address",
        )
        raise typer.Exit(code=1)
    from booking_agent.models.wire import DeliveryAddress

    return DeliveryAddress.model_validate(session.service_address).model_dump(
        by_alias=False, exclude_none=True,
    )


@book_app.callback(invoke_without_command=True)
def book(
    ctx: typer.Context,
    service_id: Optional[str] = typer.Option(None, "--service-id"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Skip create_booking; emit the planned payload only."),
) -> None:
    """Create the booking. Requires calendar gate + user-confirmed slot + link-cli buyer."""
    if ctx.invoked_subcommand is not None:
        return
    session = must_load()
    if service_id is None:
        service_id = session.service_id
    if service_id is None:
        emit_error(
            "BOOKED",
            code="missing_service_id",
            message="`--service-id` is required for `booking-agent flow book` "
            "(or run `booking-agent flow availability --service-id ...` first). "
            "(Use `booking-agent flow book poll` to poll an existing booking.)",
        )
        raise typer.Exit(code=1)
    try:
        require_business(session)
        require_calendar_gate(session)
        require_slot_selected(session)
        if not dry_run:
            require_buyer_from_link_cli(session)
    except GateError as e:
        raise _handle_gate(session, e)

    backend = resolve_backend()

    async def _refresh_catalog():
        from ..catalog import live_service_summary

        profile = await backend.resolve_profile(session.business.profile_url)
        live = await backend.get_service(profile, service_id)
        if live.id != service_id:
            raise RuntimeError(
                f"Live catalog id {live.id!r} does not match requested service_id {service_id!r}"
            )
        return live_service_summary(live)

    try:
        refreshed_live_service = asyncio.run(_refresh_catalog())
    except Exception as e:
        raise _emit_flow_http_error(
            session.flow_phase, e, default_code="catalog_fetch_failed",
        )

    from ..catalog import classify_payment_route, payment_timing_changed_refusal, payment_timing_refusal

    timing_refusal = payment_timing_refusal(refreshed_live_service)
    if timing_refusal is not None:
        emit_error(
            session.flow_phase,
            code=timing_refusal["code"],
            message=timing_refusal["message"],
        )
        raise typer.Exit(code=1)
    changed = payment_timing_changed_refusal(session, refreshed_live_service)
    if changed is not None:
        emit_error(
            session.flow_phase,
            code=changed["code"],
            message=changed["message"],
        )
        raise typer.Exit(code=1)
    classified = classify_payment_route(refreshed_live_service)
    session.payment_timing = classified["timing"]
    session.payment_route = classified["route"]
    session.live_service = refreshed_live_service
    save_session(session)
    delivery_addr = _confirmed_delivery_address(
        session, refreshed_live_service, resume_command="booking-agent flow book",
    )
    notes = session.booking_notes

    if dry_run:
        emit(_agent_out(
            session,
            prompt="Dry run: gates passed but skipping create_booking. "
                   "Re-run without --dry-run to actually book.",
            next_command="booking-agent flow book",
            data={"would_book": {
                "service_id": service_id,
                "slot_id": session.slot_id,
                "buyer_source": session.buyer_source,
                "notes": notes,
                "delivery_address": delivery_addr,
            }},
        ))
        return

    retry_command = f"booking-agent flow book --service-id {shlex.quote(service_id)}"
    try:
        from booking_agent.step_timing import timed_step_sync

        with timed_step_sync("checkout_create"):
            booking_obj, booking_ref, checkout_path = create_booking_sync(
                session, service_id, notes=notes, delivery_address=delivery_addr,
                retry_command=retry_command,
            )
    except Exception as e:
        from booking_agent.ucp_checkout import MandatoryAcceptancePending

        if isinstance(e, MandatoryAcceptancePending):
            session.last_error = LastError(
                code="mandatory_acceptance_pending",
                message=str(e),
            )
            transition(session, to="FAILED")
            save_session(session)
            emit_error(
                session.flow_phase,
                code="mandatory_acceptance_pending",
                message=str(e),
                data={
                    "continue_url": e.continue_url,
                    "links": e.links,
                    "policies": e.policies,
                    "messages": e.messages,
                },
            )
            raise typer.Exit(code=1)
        session.last_error = None
        transition(session, to="FAILED")
        save_session(session)
        raise _emit_booking_create_error(
            session.flow_phase, e,
            default_code="create_booking_failed", retry_command=retry_command,
        )

    from ..flow_timings import apply_pending_timings

    apply_pending_timings(session)

    apply_booking_to_session(session, booking_ref, checkout_path)
    if checkout_path == "ucp":
        transition(session, to="PAYMENT_PENDING")
    else:
        transition(session, to="BOOKED")
    save_session(session)

    payment_context = booking_ref.payment_context
    action_expires_at = booking_ref.action_expires_at

    at_service = session.payment_route == "at_service"
    next_pay = "booking-agent flow pay request --payment-method-id <id>"
    path_summary = payment_path_summary(
        payment_route=session.payment_route,
        checkout_path=checkout_path,
    )

    from ..catalog import merchant_quote_for_session, payment_price_for_session

    price_info = payment_price_for_session(session, payment_context=payment_context)
    merchant_quote = merchant_quote_for_session(session)

    direct = at_service or checkout_path != "ucp"
    booking_narration = None
    if at_service:
        booking_narration = (
            "Your booking request is in. Nothing was charged now; you'll pay "
            f"{price_info['price_display'] or 'the quoted price'} at the appointment. "
            "I'm now waiting for the business to confirm it."
        )
    elif direct:
        booking_narration = (
            "Your booking request is in"
            + (". It's free, so nothing was charged. " if session.payment_route == "free"
               else " and nothing was charged. ")
            + "I'm now waiting for the business to confirm it."
        )
    deposit_route = session.payment_route == "deposit_required"
    if direct:
        prompt_out = (
            "Booking request placed. Before running next_command, tell the user "
            "data.booking_narration in plain words: the request is in, nothing was charged, "
            "and you are waiting for the business to confirm. Do not create a payment. Then "
            "run `booking-agent flow book poll --wait`."
        )
    elif deposit_route:
        prompt_out = (
            "Checkout created for a deposit booking. The user approved booking and paying "
            "only the deposit at buyer resolve. Before any charge, show the deposit due now "
            "(data.due_now_display), the amount due at the appointment, and every item in "
            "data.payment_schedules and data.payment_disclosures, and get their agreement. "
            "Then run `booking-agent flow pay request` with --disclosures-confirmed."
        )
    else:
        prompt_out = (
            "UCP checkout session created. User already approved booking and charge at "
            "buyer resolve. If data.price_display matches what you quoted, proceed directly "
            "to `booking-agent flow pay request` without another confirmation STOP. If the price "
            "changed, tell the user and get fresh approval first."
        )
    data_out: Dict[str, Any] = {}
    if booking_narration:
        data_out["booking_narration"] = booking_narration
    if deposit_route and not direct:
        due_now = session.due_now_amount_minor
        data_out["due_now_display"] = (
            _format_price_display(due_now, price_info.get("currency"))
            if due_now is not None else None
        )
        data_out["payment_schedules"] = _localized_schedules(
            session, session.payment_schedules, price_info.get("currency"),
        )
        data_out["payment_disclosures"] = _localized_messages(session, session.payment_disclosures)
        prompt_out += (
            " Quote due_at_display and content_display (the localized text), never the raw "
            "due_at or content timestamps."
        )
    total_changed = bool(merchant_quote and merchant_quote.get("total_changed"))
    if total_changed and not direct:
        data_out["total_changed"] = True
        change_note = (
            "**STOP.** The business checkout total differs from the price you quoted. Tell "
            "the user the service price (data.merchant_quote.line_item_display or "
            "subtotal_display), the tax (data.merchant_quote.tax_display, only when present), "
            "and the new total (data.merchant_quote.total_display), and get their approval "
            "for that total. Then pass --total-confirmed to `booking-agent flow pay request`."
        )
        prompt_out = (
            f"{prompt_out} The deposit and balance are portions of "
            "data.merchant_quote.total_display. " + change_note
            if deposit_route
            else change_note
        )
        next_pay = f"{next_pay} --total-confirmed"
    emit(_agent_out(
        session,
        prompt=prompt_out,
        next_command="booking-agent flow book poll --wait" if direct else next_pay,
        data={
            "booking_id": booking_obj.id,
            "booking_url": booking_obj.booking_url,
            "checkout_path": checkout_path,
            "payment_context": payment_context,
            "action_expires_at": action_expires_at,
            "ucp_checkout_session_id": session.ucp_checkout_session_id,
            "ucp_spt_network_id": session.ucp_spt_network_id,
            "payment_path_summary": path_summary,
            "payment_route": session.payment_route,
            "payment_due": "at_service" if at_service else None,
            "price_display": price_info["price_display"],
            "currency": price_info.get("currency"),
            "amount_minor_units": price_info.get("amount_minor_units"),
            "price_source": price_info.get("price_source"),
            "quote_caveat": price_info.get("quote_caveat"),
            "merchant_quote": merchant_quote,
            "checkout_links": session.ucp_checkout_links,
            "checkout_policies": session.ucp_checkout_policies,
            **data_out,
        },
    ))


pay_app = typer.Typer(help="Link spend approval and UCP checkout completion.")
app.add_typer(pay_app, name="pay")

_REBOOK_MSG = (
    "Run `booking-agent flow pay rebook --service-id <id>` to create a fresh checkout, "
    "then retry payment."
)


def _read_pay_context(
    session: Session, context: Optional[str], context_file: Optional[str],
) -> Optional[str]:
    """The caller's override, or None so the CLI writes the note from the session."""
    if context is not None and context_file is not None:
        emit_error(
            session.flow_phase,
            code="context_required",
            message="Pass at most one of --context '<text>' or --context-file <path|->.",
            next_command="booking-agent flow pay request --payment-method-id <id>",
        )
        raise typer.Exit(code=1)
    if context is None and context_file is None:
        return None
    if context_file is None:
        return context
    try:
        text = sys.stdin.read() if context_file == "-" else Path(context_file).read_text()
    except OSError as e:
        emit_error(
            session.flow_phase,
            code="context_file_unreadable",
            message=f"Could not read --context-file: {e.strerror or e}",
        )
        raise typer.Exit(code=1)
    return text.strip()


@pay_app.command("request")
def pay_request(
    payment_method_id: str = typer.Option(..., "--payment-method-id"),
    context: Optional[str] = typer.Option(
        None, "--context", hidden=True,
        help="Scripts only: override the Link note the CLI writes from the booking.",
    ),
    context_file: Optional[str] = typer.Option(
        None, "--context-file", hidden=True,
        help="Scripts only: read the Link note override from this file, or '-' for stdin.",
    ),
    then_complete: bool = typer.Option(
        False,
        "--then-complete/--no-then-complete",
        help=(
            "After creating the spend request, poll Link approval and run "
            "pay ucp-complete in the same process (blocks until checkout finishes; for "
            "scripts/CI only). Default returns immediately after surfacing the approval URL "
            "once so agents can show it before polling."
        ),
    ),
    disclosures_confirmed: bool = typer.Option(
        False,
        "--disclosures-confirmed/--no-disclosures-confirmed",
        help="Required when the checkout returned payment-term disclosures the user must acknowledge.",
    ),
    total_confirmed: bool = typer.Option(
        False,
        "--total-confirmed/--no-total-confirmed",
        help=(
            "Required when the business checkout total (tax, fees, discounts) differs from "
            "the quoted price and the user approved the new total."
        ),
    ),
) -> None:
    """Create a link-cli spend request with checkout_mode-aware credential routing."""
    session = must_load()
    context = _read_pay_context(session, context, context_file)
    if session.flow_phase != "PAYMENT_PENDING":
        emit_error(
            session.flow_phase,
            code="wrong_phase",
            message="pay request requires PAYMENT_PENDING phase.",
            next_command="booking-agent flow status",
        )
        raise typer.Exit(code=1)
    _refuse_charge_after_capture(session)
    if (
        not then_complete
        and session.payment.spend_request_id
        and session.payment.spend_approval_surfaced
    ):
        existing_id = session.payment.spend_request_id
        emit_error(
            session.flow_phase,
            code="approval_already_surfaced",
            message=(
                "Approval URL was already surfaced for this session. "
                "Do not create another spend request."
            ),
            next_command="booking-agent flow pay poll",
            data={"spend_request_id": existing_id},
        )
        raise typer.Exit(code=1)
    if session.payment_route in ("free", "at_service"):
        emit_error(
            session.flow_phase,
            code="pay_not_required",
            message=(
                "This booking does not take payment now. The quoted price is due at "
                "the appointment."
                if session.payment_route == "at_service"
                else "This booking does not require payment."
            ),
            next_command="booking-agent flow book poll --wait",
        )
        raise typer.Exit(code=1)
    disclosures = list(session.payment_disclosures or [])
    schedules = list(session.payment_schedules or [])
    unrenderable = [
        d for d in disclosures
        if not (
            (isinstance(d.get("description"), dict) and d["description"].get("plain"))
            or d.get("content")
            or d.get("text")
            or d.get("message")
        )
    ]
    if unrenderable:
        continue_url = None
        for item in session.ucp_checkout_links or []:
            if isinstance(item, dict) and item.get("url"):
                continue_url = item.get("url")
                break
        emit_error(
            session.flow_phase,
            code="disclosure_unrenderable",
            message=(
                "A required disclosure could not be shown faithfully. Open the "
                "merchant continue URL."
            ),
            data={"continue_url": continue_url, "disclosures": disclosures},
        )
        raise typer.Exit(code=1)
    if (disclosures or session.payment_route == "deposit_required") and not (
        disclosures_confirmed or session.disclosures_user_confirmed
    ):
        emit_error(
            session.flow_phase,
            code="disclosures_unconfirmed",
            message=(
                "Show the due-now amount, the amount due at the appointment, and every "
                "disclosure, then re-run pay request with --disclosures-confirmed."
            ),
            data={
                "due_now_amount_minor": session.due_now_amount_minor,
                "checkout_total_minor": session.ucp_checkout_amount_minor,
                "schedules": schedules,
                "disclosures": disclosures,
            },
            next_command=(
                "booking-agent flow pay request --payment-method-id <id> "
                "--disclosures-confirmed"
            ),
        )
        raise typer.Exit(code=1)
    if disclosures_confirmed:
        session.disclosures_user_confirmed = True
        save_session(session)
    from ..catalog import checkout_total_changed, merchant_quote_for_session

    checkout_total = session.ucp_checkout_amount_minor
    if checkout_total_changed(session.ucp_merchant_quote):
        if total_confirmed:
            session.total_confirmed_amount_minor = int(checkout_total)
            save_session(session)
        elif session.total_confirmed_amount_minor != checkout_total:
            emit_error(
                session.flow_phase,
                code="checkout_total_unconfirmed",
                message=(
                    "The business checkout total differs from the quoted price. Show the "
                    "service price, tax, and total from data.merchant_quote, get the user's "
                    "approval, then re-run pay request with --total-confirmed."
                ),
                data={"merchant_quote": merchant_quote_for_session(session)},
                next_command=(
                    "booking-agent flow pay request --payment-method-id <id> "
                    "--total-confirmed"
                ),
            )
            raise typer.Exit(code=1)
    try:
        require_link_auth_gate(session)
    except GateError as e:
        raise _handle_gate(session, e)
    try:
        if not get_auth_status().authenticated:
            session.link_auth_gate_completed = False
            save_session(session)
            emit_error(
                session.flow_phase,
                code="link_auth_expired",
                message=(
                    "Link wallet session expired. Run link-auth connect, post the sign-in "
                    "link, and start link-auth poll in the background in the same turn."
                ),
                next_command="booking-agent flow link-auth connect",
            )
            raise typer.Exit(code=1)
    except Exception as e:
        emit_error(
            session.flow_phase,
            code="link_auth_status_failed",
            message=str(e),
            next_command="booking-agent flow link-auth connect",
        )
        raise typer.Exit(code=1)
    try:
        fields = resolve_spend_request_fields_sync(session)
    except Exception as e:
        emit_error(session.flow_phase, code="payment_context_missing", message=str(e))
        raise typer.Exit(code=1)
    if context is None:
        from ..booking_summary import payment_context_text

        context = payment_context_text(
            session, _format_price_display(fields["amount_minor_units"], fields["currency"]),
        )
    if len(context) < 100:
        emit_error(
            session.flow_phase,
            code="context_too_short",
            message="The Link note must be at least 100 characters for spend approval.",
            next_command="booking-agent flow pay request --payment-method-id <id>",
        )
        raise typer.Exit(code=1)

    merchant_name = session.business.name or "Merchant"
    merchant_url = session.business.profile_url or ""
    credential_type = (
        "shared_payment_token" if session.checkout_mode == "ucp_native" else None
    )
    network_id = session.ucp_spt_network_id
    if session.checkout_mode == "ucp_native" and not network_id:
        emit_error(
            session.flow_phase,
            code="missing_network_id_for_spt",
            message=(
                "UCP checkout did not include com.stripe.payments Link instrument "
                "config.network_id. The merchant must return it on checkout "
                "available_instruments."
            ),
            next_command="booking-agent flow pay rebook --service-id <id>",
        )
        raise typer.Exit(code=1)
    try:
        created = create_spend_request(
            payment_method_id=payment_method_id,
            amount=fields["amount_minor_units"],
            currency=fields["currency"],
            merchant_name=merchant_name,
            merchant_url=merchant_url,
            context=context,
            line_item_label=fields["line_item_label"],
            credential_type=credential_type,
            network_id=network_id,
        )
    except Exception as e:
        emit_error(session.flow_phase, code="spend_request_create_failed", message=str(e))
        raise typer.Exit(code=1)

    session.payment.spend_request_id = created.id
    session.payment.payment_method_id = payment_method_id
    session.payment.spend_approval_surfaced = True
    session.payment.spend_approval_user_surfaced = False
    session.payment.spend_action_surfaced = None
    save_session(session)

    if created.status == SPEND_REQUIRES_ACTION:
        action = created.next_action
        if action is None or not action.auto_resume:
            raise _require_new_spend_request(session, created.id, action)
        _emit_spend_action_awaiting(
            session,
            created.id,
            action,
            data={
                "checkout_mode": session.checkout_mode,
                "credential_type": credential_type,
                "payment_method_id": payment_method_id,
                "spend_request_command_summary": created.command_summary,
            },
        )
        if then_complete:
            session.payment.spend_approval_user_surfaced = True
            save_session(session)
            _run_payment_completion(session, created.id)
            _finish_post_payment_steps(must_load())
        return

    next_cmd, _ = _payment_pending_next(session, created.id)
    from ..catalog import payment_price_for_session

    price_info = payment_price_for_session(session)
    price_display = price_info["price_display"] or _format_price_display(
        fields["amount_minor_units"],
        fields["currency"],
    )
    approval_data = {
        "spend_request_id": created.id,
        "approval_url": created.approval_url,
        "checkout_mode": session.checkout_mode,
        "credential_type": credential_type,
        "payment_method_id": payment_method_id,
        "amount_minor_units": fields["amount_minor_units"],
        "currency": fields["currency"],
        "price_display": price_display,
        "merchant_name": merchant_name,
        "merchant_url": merchant_url,
        "spend_request_command_summary": created.command_summary,
    }

    write_spend_approval_notice(created.approval_url)

    awaiting_user, user_msg, prompt = spend_approval_surface(
        created.approval_url,
        then_complete=then_complete,
    )
    if then_complete:
        session.payment.spend_approval_user_surfaced = True
        save_session(session)
        emit(_agent_out(
            session,
            user_message=user_msg,
            prompt=prompt,
            next_command=next_cmd,
            data=approval_data,
        ))
        _run_payment_completion(session, created.id)
        _finish_post_payment_steps(must_load())
        return

    emit(_agent_out(
        session,
        awaiting_user=awaiting_user,
        user_message=user_msg,
        prompt=prompt,
        next_command=next_cmd,
        data=approval_data,
    ))


def _skill_compliance_failures(session: Session) -> list[str]:
    failures: list[str] = []
    if session.flow_phase not in ("BOOKED", "PAYMENT_PENDING", "CONFIRMED"):
        failures.append(
            f"flow_phase={session.flow_phase} (expected BOOKED/PAYMENT_PENDING/CONFIRMED)"
        )
    if not session.calendar_gate_completed:
        failures.append("calendar_gate_completed=False")
    if not session.link_auth_gate_completed:
        failures.append("link_auth_gate_completed=False")
    if not session.checkout_prereq_completed:
        failures.append("checkout_prereq_completed=False")
    if not session.slot_user_confirmed:
        failures.append("slot_user_confirmed=False")
    if session.buyer_source != BUYER_SOURCE_LINKCLI:
        failures.append(
            f"buyer_source='{session.buyer_source}' (must be '{BUYER_SOURCE_LINKCLI}')"
        )
    if _selected_slot_zones_differ(session) and not session.timezone_difference_shown:
        failures.append(
            "timezone_difference_shown=False (buyer and business timezones differ)"
        )
    return failures


def _apply_skill_compliance(session: Session) -> None:
    failures = _skill_compliance_failures(session)
    if failures:
        emit_error(
            session.flow_phase,
            code="skill_compliance_failed",
            message="Skill compliance check failed: " + "; ".join(failures),
        )
        raise typer.Exit(code=1)
    session.skill_compliance_validated = True
    save_session(session)


def _finish_post_payment_steps(session: Session) -> None:
    """After CONFIRMED, validate skill compliance and create a calendar event in-process."""
    if session.flow_phase != "CONFIRMED":
        return

    _apply_skill_compliance(session)

    calendar_data: dict = {"skipped": True}
    if session.calendar_state == "skipped" or session.calendar_mode == CALENDAR_MODE_NONE:
        calendar_data = {"skipped": True}
    elif session.calendar_mode == CALENDAR_MODE_FREEBUSY:
        calendar_data = {"skipped": True, "reason": "freebusy_only"}
    elif session.calendar_mode == CALENDAR_MODE_EVENTS:
        result = create_calendar_event(session, user_key=_CALENDAR_SESSION_ID)
        if not result.get("ok"):
            emit_error(
                session.flow_phase,
                code=result.get("code", "calendar_write_failed"),
                message=result.get("message", "Failed to create calendar event."),
            )
            raise typer.Exit(code=1)
        calendar_data = {
            "skipped": False,
            "event_id": result.get("event_id"),
            "html_link": result.get("html_link"),
            "idempotent": result.get("idempotent", False),
        }

    html_link = calendar_data.get("html_link")
    booking_url = session.booking.booking_url
    prompt = (
        "Payment complete and booking confirmed. Tell the user they are booked in everyday "
        "language (service, time, business). Do not mention CLI commands, phases, or payment "
        "internals."
    )
    if html_link:
        prompt += (
            f" Show the user the raw html_link URL (never markdown [label](url)): {html_link}"
            " The event description is already a summary of this booking, including payment "
            "status and how to cancel or reschedule."
        )
    if booking_url:
        # booking_url is the only out-of-band handle to this booking: the
        # booking-scoped credential lives in local session state and cannot be
        # recovered on another device, so if the user ever wants to view, change,
        # or cancel the booking without this agent they need this URL. Surface it
        # at success (not just on the failure/cancel path) so it is captured while
        # it still exists. The calendar description already includes it.
        prompt += (
            f" Show the user the raw booking_url URL (never markdown [label](url)): {booking_url} "
            "and recommend they save it. It is how they can view, cancel, or reschedule this "
            "booking later, including from another device."
        )
        if html_link:
            prompt += " That link is already in the calendar event description."

    emit(_agent_out(
        session,
        prompt=prompt,
        data={
            "booking_id": session.booking.id,
            "booking_url": booking_url,
            "payment_status": session.payment.status,
            "skill_compliance_validated": True,
            "calendar": calendar_data,
        },
    ))


_CAPTURED_POLL_INTERVAL_S = 2.0
_CAPTURED_CONFIRM_TIMEOUT_S = 300.0


@pay_app.command("poll")
def pay_poll() -> None:
    """Ack that the approval URL was shown, poll Link, and finish the booking.

    Start in the background right after ``pay request`` so the agent can post
    ``user_message`` (the approval URL) in the same turn. Sets ``spend_approval_user_surfaced``, blocks until checkout finishes, then runs
    skill-compliance validation and calendar create-event when applicable.

    A captured payment the business has not confirmed yet is not a failure: the
    command reports it once and keeps waiting for the booking, exiting 0 when it
    confirms and nonzero only on timeout or cancellation.
    """
    session = must_load()
    if session.flow_phase != "PAYMENT_PENDING":
        emit_error(
            session.flow_phase,
            code="wrong_phase",
            message="pay poll requires PAYMENT_PENDING phase.",
            next_command="booking-agent flow status",
        )
        raise typer.Exit(code=1)
    sr_id = session.payment.spend_request_id
    if not sr_id:
        emit_error(
            session.flow_phase,
            code="no_spend_request",
            message="No spend request on session. Run `booking-agent flow pay request` first.",
            next_command="booking-agent flow pay request --payment-method-id <id>",
        )
        raise typer.Exit(code=1)
    if not session.payment.spend_approval_surfaced:
        emit_error(
            session.flow_phase,
            code="approval_not_created",
            message="Spend approval URL was not surfaced yet. Run `booking-agent flow pay request`.",
            next_command="booking-agent flow pay request --payment-method-id <id>",
        )
        raise typer.Exit(code=1)
    session.payment.spend_approval_user_surfaced = True
    save_session(session)
    try:
        _run_payment_completion(session, sr_id, continue_on_captured=True)
    except _CapturedAwaitingConfirmation:
        book_poll(
            booking_id=None, wait=True, poll_interval=_CAPTURED_POLL_INTERVAL_S,
            timeout=_CAPTURED_CONFIRM_TIMEOUT_S,
        )
        return
    _finish_post_payment_steps(must_load())


@pay_app.command("ack-surface")
def pay_ack_surface() -> None:
    """Acknowledge that the agent posted the Link approval URL to the user."""
    session = must_load()
    if session.flow_phase != "PAYMENT_PENDING":
        emit_error(
            session.flow_phase,
            code="wrong_phase",
            message="pay ack-surface requires PAYMENT_PENDING phase.",
            next_command="booking-agent flow status",
        )
        raise typer.Exit(code=1)
    if not session.payment.spend_request_id:
        emit_error(
            session.flow_phase,
            code="no_spend_request",
            message="No spend request on session. Run `booking-agent flow pay request` first.",
            next_command="booking-agent flow pay request --payment-method-id <id>",
        )
        raise typer.Exit(code=1)
    if not session.payment.spend_approval_surfaced:
        emit_error(
            session.flow_phase,
            code="approval_not_created",
            message="Spend approval URL was not surfaced yet. Run `booking-agent flow pay request`.",
            next_command="booking-agent flow pay request --payment-method-id <id>",
        )
        raise typer.Exit(code=1)
    session.payment.spend_approval_user_surfaced = True
    save_session(session)
    sr_id = session.payment.spend_request_id
    next_cmd, prompt = _payment_pending_next(session, sr_id)
    emit(_agent_out(
        session,
        prompt=prompt,
        next_command=next_cmd,
        data={"spend_request_id": sr_id},
    ))


@pay_app.command("rebook")
def pay_rebook(
    service_id: str = typer.Option(..., "--service-id"),
) -> None:
    """Create a fresh checkout for the same slot, buyer, address, and note."""
    session = must_load()
    _refuse_charge_after_capture(session)
    try:
        require_business(session)
        require_calendar_gate(session)
        require_slot_selected(session)
        require_buyer_from_link_cli(session)
    except GateError as e:
        raise _handle_gate(session, e)

    prior_spend = session.payment.spend_request_id
    retry_command = f"booking-agent flow pay rebook --service-id {shlex.quote(service_id)}"
    delivery_addr = _confirmed_delivery_address(
        session, session.live_service, resume_command=retry_command,
    )
    notes = session.booking_notes
    try:
        from booking_agent.step_timing import timed_step_sync

        with timed_step_sync("checkout_create"):
            booking_obj, booking_ref, checkout_path = create_booking_sync(
                session, service_id, notes=notes, delivery_address=delivery_addr,
                retry_command=retry_command,
            )
    except Exception as e:
        save_session(session)
        raise _emit_booking_create_error(
            session.flow_phase, e, default_code="rebook_failed", retry_command=retry_command,
        )

    from ..flow_timings import apply_pending_timings

    apply_pending_timings(session)

    apply_booking_to_session(session, booking_ref, checkout_path)
    transition(session, to="PAYMENT_PENDING")
    save_session(session)

    emit(_agent_out(
        session,
        prompt=(
            "Fresh checkout created. Create a new spend request for it with "
            "`booking-agent flow pay request`."
        ),
        next_command="booking-agent flow pay request --payment-method-id <id>",
        data={
            "booking_id": booking_obj.id,
            "action_expires_at": booking_ref.action_expires_at,
            "spend_request_id": prior_spend,
            "checkout_path": checkout_path,
        },
    ))


@pay_app.command("ucp-complete")
def pay_ucp_complete(
    handler_id: Optional[str] = typer.Option(
        None,
        "--handler-id",
        help=(
            "Payment handler id from profile or checkout. Omit when GET checkout exposes "
            "exactly one payment.instruments handler_id or the profile lists one payment_handlers id."
        ),
    ),
    spend_request_id: Optional[str] = typer.Option(
        None,
        "--spend-request-id",
        help="Approved spend request id (defaults to the one from `pay request`). Used to retrieve SPT via link-cli.",
    ),
    approval_timeout: int = typer.Option(600, "--approval-timeout"),
    poll_interval: float = typer.Option(2.0, "--poll-interval"),
) -> None:
    """Complete UCP checkout using SPT from link-cli spend-request retrieve after user approval.

    Handler id is taken from ``--handler-id`` when set; otherwise resolved from the checkout
    session (``payment.instruments``) or a single ``payment_handlers`` entry on the business
    profile (GH-004).
    """
    session = must_load()
    if session.flow_phase != "PAYMENT_PENDING":
        emit_error(
            session.flow_phase,
            code="wrong_phase",
            message="ucp-complete requires PAYMENT_PENDING phase.",
            next_command="booking-agent flow status",
        )
        raise typer.Exit(code=1)
    if session.booking.checkout_path != "ucp":
        emit_error(
            session.flow_phase,
            code="wrong_checkout_path",
            message="This session is not UCP checkout.",
            next_command="booking-agent flow status",
        )
        raise typer.Exit(code=1)
    _pay_ucp_complete_impl(
        session,
        handler_id=handler_id,
        spend_request_id=spend_request_id,
        approval_timeout=approval_timeout,
        poll_interval=poll_interval,
    )


def _pay_ucp_complete_impl(
    session: Session,
    *,
    handler_id: Optional[str],
    spend_request_id: Optional[str],
    approval_timeout: int = 600,
    poll_interval: float = 2.0,
    continue_on_captured: bool = False,
) -> None:
    _refuse_charge_after_capture(session)
    sr_ucp = spend_request_id or session.payment.spend_request_id
    if not sr_ucp:
        emit_error(
            session.flow_phase,
            code="no_spend_request_for_ucp",
            message=(
                "UCP checkout needs a spend request id (SPT credential type). "
                "Run `booking-agent flow pay request` or pass --spend-request-id."
            ),
            next_command="booking-agent flow pay request --payment-method-id <id>",
        )
        raise typer.Exit(code=1)
    if spend_request_id:
        session.payment.spend_request_id = spend_request_id
        save_session(session)

    sr_ucp = spend_request_id or session.payment.spend_request_id
    _require_spend_approval_surfaced_to_user(session)
    _poll_spend_approval_or_exit(
        session,
        sr_ucp,
        approval_timeout=approval_timeout,
        poll_interval=poll_interval,
        next_command=f"booking-agent flow pay ucp-complete --spend-request-id {sr_ucp}",
    )

    async def _go():
        from booking_agent.step_timing import timed_step
        from ..payment_helpers import complete_ucp_checkout_async

        async with timed_step("pay"):
            return await complete_ucp_checkout_async(session, handler_id=handler_id)

    try:
        result = asyncio.run(_go())
    except UcpCompleteOutcomeError as outcome:
        raise _handle_complete_outcome(
            session, outcome, spend_request_id or session.payment.spend_request_id,
            continue_on_captured=continue_on_captured,
        )
    except Exception as e:
        sr_retry = spend_request_id or session.payment.spend_request_id
        msg = str(e)
        lowered = msg.lower()
        business = _business_label(session)
        if isinstance(e, SharedPaymentTokenUnavailable):
            code = "shared_payment_token_not_issued"
            recovery = (
                f"{msg} Nothing was charged. Re-run pay ucp-complete once; if the token is "
                "still missing, Link cannot issue a credential for this business's payment "
                "profile."
            )
            user_message = (
                "Your approval went through in Link, but Link didn't issue the payment "
                f"credential {business} needs, so nothing was charged. I'm trying once more, "
                "and I'll tell you if this business can't take a Link payment right now."
            )
        elif "timeout" in lowered or "max-attempts" in lowered or "approval" in lowered:
            code = "approval_timeout"
            recovery = (
                "Link approval not received in time. Re-run pay ucp-complete to poll again."
            )
            user_message = (
                "I haven't seen your approval in Link yet. Approve the payment request in "
                "your Link app and I'll finish the booking."
            )
        elif "pending" in lowered or "not approved" in lowered:
            code = "spend_request_not_approved"
            recovery = msg
            user_message = (
                "Your payment request in Link isn't approved yet. Approve it in your Link "
                "app to finish the booking, or tell me if you'd rather stop."
            )
        else:
            code = "ucp_complete_failed"
            recovery = msg
            user_message = (
                f"I couldn't finish the payment with {business} just now. I'm trying "
                "again, and I'll tell you what to do if it still doesn't work."
            )
        next_cmd = (
            f"booking-agent flow pay ucp-complete --spend-request-id {sr_retry}"
            if sr_retry
            else "booking-agent flow pay request --payment-method-id <id>"
        )
        emit_error(
            session.flow_phase,
            code=code,
            message=recovery + " Post user_message to the user, then run next_command.",
            user_message=user_message,
            next_command=next_cmd,
            data={"spend_request_id": sr_retry} if sr_retry else None,
        )
        raise typer.Exit(code=1)

    sr_ucp = spend_request_id or session.payment.spend_request_id
    result = {
        **result,
        "spt_source": "link-cli spend-request retrieve",
        "handler_id_resolved": result.get("handler_id_resolved"),
    }

    order_id_val = str(result.get("order_id") or "")
    session.payment.order_id = order_id_val or None
    _record_new_card_retry_success(session, sr_ucp)

    from ..flow_timings import apply_pending_timings

    apply_pending_timings(session)

    from ..engine import transition

    session.payment.status = "paid"
    transition(session, to="CONFIRMED")
    save_session(session)
    emit(_agent_out(
        session,
        prompt="UCP checkout completed. Poll booking or validate compliance.",
        next_command="booking-agent flow book poll --wait",
        data=result,
    ))


_NEW_CARD_NEXT_COMMAND = (
    "booking-agent flow pay request --payment-method-id <id>"
)


def _business_label(session: Session) -> str:
    return session.business.name or "the business"


def _payment_captured(session: Session) -> bool:
    return session.payment.captured_unconfirmed or session.payment.status == "paid"


def _emit_captured_payment_handoff(session: Session, *, canceled: bool) -> "typer.Exit":
    """Tell the user how to reach the business about money taken without a booking.

    The hosted checkout page is left out: here it is a payment page, and the
    user has already paid.
    """
    checkout = (
        asyncio.run(_read_ucp_checkout_for_handoff(session))
        if session.ucp_checkout_session_id
        else None
    )
    handoff = _merchant_cancel_handoff(session, checkout)
    contact_urls = [u for u in handoff.pop("cancel_urls") if u["type"] != "checkout"]
    business = _business_label(session)
    if canceled:
        lines = [
            f"{business} canceled this booking after your payment went through. Contact "
            "them to ask for your refund:",
        ]
    else:
        lines = [
            f"Your payment to {business} went through, but they still haven't confirmed "
            "your booking. Please don't pay again. Contact them and ask them to confirm "
            "the booking, or to refund you if they can't:",
        ]
    if contact_urls:
        lines.append(contact_urls[0]["url"])
    if session.booking.id:
        lines.append(f"Booking reference: {session.booking.id}")
    if handoff.get("order_id"):
        lines.append(f"Order reference: {handoff['order_id']}")
    lines.append(
        "If they don't sort it out, tell me and I'll give you the payment details you "
        "need to dispute the charge."
    )
    emit_error(
        session.flow_phase,
        code="booking_canceled_after_payment" if canceled else "payment_captured_booking_unconfirmed",
        message=(
            "The user was charged and the business "
            + ("canceled the booking. " if canceled else "has not confirmed the booking. ")
            + "Post user_message as is (raw URL, never markdown [label](url)). Never retry "
            "the payment or ask for another card. If the user asks to dispute the charge, "
            "run `booking-agent flow evidence` and share its payment and booking references."
            + (
                ""
                if canceled
                else " The user may ask you to check again later with next_command."
            )
        ),
        user_message="\n".join(lines),
        next_command=None if canceled else "booking-agent flow book poll --wait",
        forbidden=["booking-agent flow pay ucp-complete", "booking-agent flow pay request"],
        data={
            "booking_id": session.booking.id,
            "contact_urls": contact_urls,
            **handoff,
        },
    )
    return typer.Exit(code=1)


class _CapturedAwaitingConfirmation(Exception):
    """Payment captured; the caller keeps waiting for the business in-process."""


def _captured_waiting_message(business: str) -> str:
    return (
        f"Your payment to {business} went through, but they haven't confirmed "
        "your booking yet. This can take a couple of minutes, and it usually "
        "goes through on its own. Please wait while I keep checking with them. "
        "I'll let you know as soon as it's confirmed. There's no need to contact "
        f"{business} yet, and please don't pay again."
    )


def _handle_complete_outcome(
    session: Session,
    outcome: UcpCompleteOutcomeError,
    spend_request_id: Optional[str],
    *,
    continue_on_captured: bool = False,
) -> Exception:
    """Route a classified complete_checkout failure (spec #93 A1-A5)."""
    same_sr_command = (
        f"booking-agent flow pay ucp-complete --spend-request-id {spend_request_id}"
        if spend_request_id
        else _NEW_CARD_NEXT_COMMAND
    )
    business = _business_label(session)
    if outcome.outcome is CompleteOutcome.DECLINED:
        return _emit_declined_spt(session, outcome, spend_request_id)
    if outcome.outcome is CompleteOutcome.IN_PROGRESS:
        save_session(session)
        emit_error(
            session.flow_phase,
            code=outcome.code,
            message=(
                "The business is still completing this checkout. This is not a decline. "
                "Post user_message, then run next_command to check the same checkout "
                "again. Do not create a new spend request or start a new charge."
            ),
            user_message=(
                f"{business} is still processing your payment. I'm checking again now. "
                "Please don't pay again."
            ),
            next_command=same_sr_command,
            data={"spend_request_id": spend_request_id, "checkout_id": outcome.checkout_id},
        )
        return typer.Exit(code=1)
    if outcome.outcome is CompleteOutcome.CAPTURED_UNCONFIRMED:
        session.payment.status = "paid"
        session.payment.captured_unconfirmed = True
        save_session(session)
        if continue_on_captured:
            emit(_agent_out(
                session,
                prompt=(
                    "The payment was captured and this same command is now waiting for the "
                    "business to confirm the booking. Post user_message once, then keep "
                    "waiting for this command to exit; do not run another command or sleep. "
                    "Its final output is the booking result."
                ),
                user_message=_captured_waiting_message(business),
                data={
                    "status": "waiting_for_business_confirmation",
                    "checkout_id": outcome.checkout_id,
                    "booking_id": session.booking.id,
                },
            ))
            return _CapturedAwaitingConfirmation()
        emit_error(
            session.flow_phase,
            code="payment_captured_booking_unconfirmed",
            message=(
                "The payment was captured but the business has not confirmed the booking. "
                "Post user_message now, before running next_command, so the user knows "
                "you are waiting and does not contact the business early. Do not retry "
                "the payment and do not ask for another card. Run next_command in the "
                "foreground to wait for the business; it finishes the booking if it "
                "confirms, or returns how the user can reach the business if it does not."
            ),
            user_message=_captured_waiting_message(business),
            next_command="booking-agent flow book poll --wait",
            forbidden=["booking-agent flow pay ucp-complete", "booking-agent flow pay request"],
            data={
                "spend_request_id": spend_request_id,
                "checkout_id": outcome.checkout_id,
                "booking_id": session.booking.id,
            },
        )
        return typer.Exit(code=1)
    if outcome.outcome is CompleteOutcome.PAYMENT_UNRECOVERABLE:
        session.payment.status = "failed"
        session.last_error = LastError(code="payment_unrecoverable", message=str(outcome))
        transition(session, to="FAILED")
        save_session(session)
        emit_error(
            session.flow_phase,
            code="payment_unrecoverable",
            message=(
                "The business reported an unrecoverable payment failure, so this checkout "
                "can no longer be completed. Post user_message, then stop. Do not retry "
                "this checkout."
            ),
            user_message=(
                f"{business} couldn't complete this checkout, so the booking wasn't finished."
            ),
            forbidden=[
                "booking-agent flow pay ucp-complete",
                "booking-agent flow pay request",
            ],
            data={
                "spend_request_id": spend_request_id,
                "checkout_id": outcome.checkout_id,
                "severity": outcome.severity,
            },
        )
        return typer.Exit(code=1)
    review_urls = [
        url
        for url in [outcome.continue_url]
        + [item.get("url") for item in outcome.links if isinstance(item, dict)]
        if isinstance(url, str) and url.strip()
    ]
    emit_error(
        session.flow_phase,
        code="mandatory_acceptance_pending",
        message=(
            "The business requires the user to accept a policy or finish another "
            "non-payment step. Post user_message (continue_url and the policy links), "
            "wait for the user to say they finished, then run next_command. Never "
            "accept for the user."
        ),
        user_message="\n".join([
            f"Before your payment can go through, {business} needs you to review and "
            "accept their terms:",
            *dict.fromkeys(review_urls),
            "Let me know once you've done that and I'll finish the booking.",
        ]),
        next_command=same_sr_command,
        data={
            "continue_url": outcome.continue_url,
            "links": outcome.links,
            "policies": outcome.policies,
            "messages": outcome.messages,
        },
    )
    return typer.Exit(code=1)


def _emit_declined_spt(
    session: Session,
    outcome: UcpCompleteOutcomeError,
    spend_request_id: Optional[str],
) -> "typer.Exit":
    """The SPT is spent: ask for a different card on a new spend request.

    Never offers the hosted ``continue_url`` (A3) and never the spend request's
    ``action_url`` (A5): after a failed complete that URL is the charge-side
    challenge, and finishing it can capture a payment with no booking.
    """
    from booking_agent.observability import emit_event, money_path_fields

    if spend_request_id:
        try:
            sr = retrieve_spend_request(spend_request_id)
        except Exception:
            sr = None
        if sr is not None and sr.status == "requires_action":
            emit_event(
                "post_failure_action_suppressed",
                checkout_id=outcome.checkout_id,
                next_action_type=sr.next_action.type if sr.next_action else None,
                **money_path_fields(
                    spend_request_id=spend_request_id, booking_id=session.booking.id,
                ),
            )

    session.payment.declined_spend_request_id = spend_request_id
    session.payment.declined_code = outcome.code
    session.payment.declined_checkout_id = outcome.checkout_id
    session.payment.spend_request_id = None
    session.payment.spend_approval_surfaced = False
    session.payment.spend_approval_user_surfaced = False
    session.payment.spend_action_surfaced = None
    session.complete_checkout_idempotency_key = None
    save_session(session)
    emit_error(
        session.flow_phase,
        code=outcome.code,
        message=(
            "The payment did not go through and this spend request is used up. Post "
            "user_message (the business's explanation) to the user, ask them to choose a "
            "different Link card, and create a new spend request with their approval. "
            "Do not retry this spend request, and do not send the user to any business "
            "checkout page or bank verification link."
        ),
        user_message=(
            f"{outcome.content or 'Your payment did not go through.'}\n"
            "Please choose a different card from your Link wallet, and I'll send you a new "
            "payment approval for this booking."
        ),
        next_command=_NEW_CARD_NEXT_COMMAND,
        data={
            "declined_spend_request_id": spend_request_id,
            "checkout_id": outcome.checkout_id,
            "severity": outcome.severity,
        },
    )
    return typer.Exit(code=1)


def _record_new_card_retry_success(session: Session, spend_request_id: Optional[str]) -> None:
    """Close out a prior decline once a different spend request completes checkout (A7.1)."""
    from booking_agent.observability import emit_event

    declined = session.payment.declined_spend_request_id
    if not declined or declined == spend_request_id:
        return
    emit_event(
        "declined_spt_retry_succeeded",
        declined_spend_request_id=declined,
        spend_request_id=spend_request_id,
        message_code=session.payment.declined_code,
        checkout_id=session.payment.declined_checkout_id,
        merchant_profile_url=session.business.profile_url,
        merchant_name=session.business.name,
        booking_id=session.booking.id,
    )
    session.payment.declined_spend_request_id = None
    session.payment.declined_code = None
    session.payment.declined_checkout_id = None


def _run_payment_completion(
    session: Session, spend_request_id: str, *, continue_on_captured: bool = False,
) -> None:
    """Poll Link approval and complete payment (used by pay request --then-complete)."""
    _pay_ucp_complete_impl(
        session,
        handler_id=None,
        spend_request_id=spend_request_id,
        continue_on_captured=continue_on_captured,
    )


@book_app.command("poll")
def book_poll(
    booking_id: Optional[str] = typer.Option(None, "--booking-id"),
    wait: bool = typer.Option(
        False, "--wait", help="Block in-process until the booking reaches a terminal status."
    ),
    poll_interval: float = typer.Option(
        1.0, "--poll-interval", help="Seconds between get_booking calls when --wait is set."
    ),
    timeout: float = typer.Option(
        120.0, "--timeout", help="Max seconds to block when --wait is set (then poll_timeout)."
    ),
) -> None:
    """Poll the booking once, or (with --wait) block until a terminal status."""
    session = must_load()
    bid = booking_id or session.booking.id
    if not bid:
        emit_error(
            session.flow_phase,
            code="no_booking_id",
            message="No booking_id in session. Run `booking-agent flow book` first.",
            next_command="booking-agent flow book",
        )
        raise typer.Exit(code=1)

    try:
        if wait:
            from booking_agent.step_timing import timed_step_sync

            with timed_step_sync("confirm"):
                b = wait_for_booking_terminal(
                    session,
                    deadline_ts=time.time() + timeout,
                    poll_interval=poll_interval,
                )
            from ..flow_timings import apply_pending_timings

            apply_pending_timings(session)
        else:
            b = fetch_booking(session, bid)
    except BookingPollTimeout as e:
        from ..booking_poll import emit_payment_unconfirmed_on_poll_timeout

        emit_payment_unconfirmed_on_poll_timeout(session)
        session.payment.status = session.payment.status or "pending"
        save_session(session)
        if _payment_captured(session):
            raise _emit_captured_payment_handoff(session, canceled=False)
        emit_error(
            session.flow_phase,
            code="poll_timeout",
            message=str(e),
            next_command="booking-agent flow book poll --wait",
        )
        raise typer.Exit(code=1)
    except BookingServerError as e:
        # The business server returned an error body (HTTP 200 with error messages).
        # Map known server error codes to actionable agent guidance.
        if e.code == "not_found":
            emit_error(
                session.flow_phase,
                code="booking_not_found",
                message=(
                    "The booking was not found on the business server. "
                    "This typically means the payment window expired and the booking "
                    "was automatically cancelled. "
                    + _REBOOK_MSG
                ),
                next_command="booking-agent flow pay rebook --service-id <id>",
            )
        else:
            emit_error(
                session.flow_phase,
                code=f"poll_failed_server_error_{e.code}",
                message=f"Business server returned error '{e.code}': {e.server_message}",
            )
        raise typer.Exit(code=1)
    except Exception as e:
        emit_error(session.flow_phase, code="poll_failed", message=str(e))
        raise typer.Exit(code=1)

    was_captured_unconfirmed = session.payment.captured_unconfirmed
    status_val = apply_booking_status(session, b)
    if was_captured_unconfirmed and session.flow_phase == "CONFIRMED":
        session.payment.captured_unconfirmed = False
    save_session(session)

    if was_captured_unconfirmed and session.flow_phase == "CONFIRMED":
        _finish_post_payment_steps(session)
        return
    if was_captured_unconfirmed and status_val in ("canceled", "no_show"):
        raise _emit_captured_payment_handoff(session, canceled=True)

    emit(_agent_out(
        session,
        prompt=f"Booking {bid} status: {status_val}.",
        data={"booking_id": bid, "status": status_val},
    ))


@book_app.command("cancel")
def book_cancel(
    booking_id: Optional[str] = typer.Option(None, "--booking-id"),
    reason: Optional[str] = typer.Option(None, "--reason", help="Cancellation reason."),
) -> None:
    """Cancel the booking using its booking-scoped credential.

    ``usp_bookings_cancel`` is privileged_scoped: authority comes from the
    credential issued when the booking was created, plus a fresh
    proof-of-possession over the key it is bound to.  If the credential is gone
    or expired there is nothing to retry as -- the platform has no standing
    identity at this business -- so the user is handed off to ``booking_url``.
    """
    session = must_load()
    bid = booking_id or session.booking.id
    if not bid:
        emit_error(
            session.flow_phase,
            code="no_booking_id",
            message="No booking_id in session. Run `booking-agent flow book` first.",
            next_command="booking-agent flow book",
        )
        raise typer.Exit(code=1)

    recorded = session.booking.status if bid == session.booking.id else None
    if recorded in INACTIVE_BOOKING_STATUSES:
        # A finished booking's credential is already retired, so the business
        # could only reject a repeat cancel (401 pop_key_mismatch).
        emit(_agent_out(
            session,
            user_message=_already_inactive_message(recorded),
            prompt=f"Booking {bid} is already {recorded}. Post user_message to the user as is.",
            data={
                "booking_id": bid,
                "status": recorded,
                "cancellation": None,
                "refund_follow_up": None,
                "calendar": None,
            },
        ))
        return

    try:
        status_val, settlement = _cancel_and_record(session, bid, reason)
    except _CancelNeedsHuman as e:
        emit_error(
            session.flow_phase,
            code=e.code,
            message=e.message,
            next_command=None,
            data={"booking_id": bid, "booking_url": e.booking_url, **e.handoff},
        )
        raise typer.Exit(code=1)
    except _CancelFailed as e:
        emit_error(
            session.flow_phase,
            code="cancel_failed",
            message=e.message,
            data={"booking_id": bid, **e.data},
        )
        raise typer.Exit(code=1)

    follow_up = settlement.get("refund_follow_up")
    calendar = _remove_calendar_event_after_cancel(session) if status_val == "canceled" else None
    user_message = (
        _cancel_user_message(session, settlement)
        if status_val == "canceled"
        else _already_inactive_message(status_val)
    )
    if calendar and calendar.get("user_note"):
        user_message += "\n" + calendar["user_note"]
    prompt = (
        f"Booking {bid} is now {status_val}. Post user_message to the user as is, with its "
        "raw URLs (never markdown [label](url)). It already says whether money was refunded "
        "and, when it was not, the numbered steps to ask the business for the refund."
    )
    if follow_up:
        prompt += (
            " Do not shorten those refund steps: the user needs the business link, the "
            "amount to ask for, and the booking and order references."
        )
    emit(_agent_out(
        session,
        user_message=user_message,
        prompt=prompt,
        data={
            "booking_id": bid,
            "status": status_val,
            "cancellation": settlement.get("cancellation"),
            "refund_follow_up": follow_up,
            "calendar": calendar,
        },
    ))


def _already_inactive_message(status: str) -> str:
    return f"This booking is already {status.replace('_', ' ')}, so there was nothing to cancel."


class _CancelNeedsHuman(Exception):
    """The agent cannot complete this cancel: the user must finish it in a browser."""

    def __init__(
        self,
        code: str,
        message: str,
        booking_url: Optional[str],
        handoff: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.booking_url = booking_url
        self.handoff = handoff or {}


class _CancelFailed(Exception):
    """The cancel did not go through; ``data`` still carries the buyer's way out."""

    def __init__(self, message: str, data: Dict[str, Any]):
        super().__init__(message)
        self.message = message
        self.data = data


def _cancel_and_record(
    session: Session, booking_id: str, reason: Optional[str],
) -> tuple[str, Dict[str, Any]]:
    """Cancel one booking and persist the outcome on its session.

    Raises ``_CancelNeedsHuman`` for a handoff and ``_CancelFailed`` for every
    other failure, so no HTTP error escapes as a traceback.
    """
    http_status: Optional[int] = None
    try:
        booking, fee = asyncio.run(_cancel_booking_async(session, booking_id, reason))
    except _CancelNeedsHuman:
        save_session(session)
        raise
    except httpx.HTTPStatusError as e:
        http_status = e.response.status_code
        message = f"The business returned HTTP {http_status} for the cancellation."
    except UcpCheckoutHttpError as e:
        http_status = e.status
        message = str(e)
    except httpx.HTTPError as e:
        message = f"Could not reach the business to cancel ({type(e).__name__})."
    except (BookingServerError, UcpProfileValidationError) as e:
        message = str(e)
    else:
        status_val = getattr(booking.status, "value", booking.status)
        session.booking.status = status_val
        save_session(session)
        # Terminal state: the credential is invalid from here, and the key can go
        # once nothing else at this business is still bound to it.
        _retire_booking_credential(session)
        save_session(session)
        settlement = fee if isinstance(fee, dict) else _cancel_settlement(session, booking, None)
        return status_val, settlement

    save_session(session)
    data: Dict[str, Any] = {
        "booking_url": session.booking.booking_url,
        **_merchant_cancel_handoff(session, None),
    }
    if http_status is not None:
        data["http_status"] = http_status
    raise _CancelFailed(message, data)


def _minor_units(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record_field(record: Any, name: str) -> Any:
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


def _cancel_settlement(
    session: Session,
    booking: Any,
    checkout: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """What the buyer still has to do after the booking itself is canceled.

    USP §5.3.5 cancels the appointment and reports ``fee`` and
    ``refund_amount``. It does not promise the refund. The hosted booking site
    splits the same two steps: Manage Booking cancels, and the buyer then
    contacts the business for the money. When the recorded refund is short of
    what was captured, say so and point at ``booking_url``, which is the
    protocol's manage page and the link that email button opens.
    """
    from ..calendar_event import _currency as _booking_currency
    from ..catalog import _format_price_display

    cancellation = _record_field(booking, "cancellation")
    fee = _minor_units(_record_field(cancellation, "fee"))
    refund = _minor_units(_record_field(cancellation, "refund_amount"))
    captured = _captured_amount_minor(session, booking)
    currency = _booking_currency(session)
    manage_url = _record_field(booking, "booking_url") or session.booking.booking_url
    order = checkout.get("order") if isinstance(checkout, dict) else None
    order_id = (order.get("id") if isinstance(order, dict) else None) or session.payment.order_id
    follow_up = None
    if captured and (refund is None or refund < captured):
        outstanding = captured - (refund or 0)
        follow_up = {
            "required": True,
            "captured_amount_minor": captured,
            "refund_amount": refund,
            "outstanding_amount_minor": outstanding,
            "currency": currency,
            "captured_display": _format_price_display(captured, currency),
            "refund_display": _format_price_display(refund, currency) if refund else None,
            "outstanding_display": _format_price_display(outstanding, currency),
            "manage_url": manage_url,
            "contact_url": manage_url or _merchant_site_url(session),
            "booking_id": session.booking.id or _record_field(booking, "id"),
            "order_id": order_id,
        }
        follow_up["steps"] = _refund_steps(session, follow_up)
    return {
        "cancellation": {"fee": fee, "refund_amount": refund},
        "captured_amount_minor": captured,
        "captured_display": _format_price_display(captured, currency) if captured else None,
        "refund_display": _format_price_display(refund, currency) if refund else None,
        "refund_follow_up": follow_up,
    }


def _remove_calendar_event_after_cancel(session: Session) -> Optional[Dict[str, Any]]:
    """Delete the cancelled booking's Google event. Never fails the cancel itself."""
    try:
        result = delete_calendar_event(session, user_key=_CALENDAR_SESSION_ID)
    except Exception as e:
        result = {
            "ok": False,
            "status": "failed",
            "code": "calendar_delete_failed",
            "message": f"Calendar cleanup failed ({type(e).__name__}).",
        }
    if result.get("status") == "skipped":
        return None
    if result.get("status") == "deleted":
        result["user_note"] = "I also removed it from your Google Calendar."
    elif result.get("status") == "failed":
        link = result.get("html_link")
        result["user_note"] = (
            "I couldn't remove it from your Google Calendar, so please delete that event "
            "yourself" + (f": {link}" if link else ".")
        )
    return result


_CAPTURED_PAYMENT_STATUSES = ("paid", "deposit_paid", "partially_refunded")


def _captured_amount_minor(session: Session, booking: Any) -> Optional[int]:
    """Money the buyer actually paid for this booking, in minor units.

    Free and pay-at-the-appointment bookings never collect anything here. A paid
    deposit is the immediate schedule amount; a booking paid in full is the
    checkout total the spend request was created for.
    """
    from ..calendar_event import _payment_route

    if _payment_route(session) in ("free", "at_service"):
        return None
    payment = _record_field(booking, "payment")
    booking_paid = str(_record_field(payment, "status") or "") in _CAPTURED_PAYMENT_STATUSES
    if not (booking_paid or _payment_captured(session) or session.payment.order_id):
        return None
    if session.due_now_amount_minor is not None:
        return int(session.due_now_amount_minor)
    if session.ucp_checkout_amount_minor is not None:
        return int(session.ucp_checkout_amount_minor)
    if booking_paid:
        return (
            _minor_units(_record_field(payment, "deposit_amount"))
            or _minor_units(_record_field(payment, "amount"))
        )
    return None


def _refund_steps(session: Session, follow_up: Dict[str, Any]) -> List[str]:
    """Plain steps the buyer takes to get money back from the business."""
    business = _business_label(session)
    owed = follow_up.get("outstanding_display") or "the amount you paid"
    contact = follow_up.get("contact_url")
    if follow_up.get("manage_url"):
        steps = [f"Open {business}'s booking page: {contact}"]
    elif contact:
        steps = [f"Go to {business}'s website and find their contact details: {contact}"]
    else:
        steps = [f"Contact {business} directly."]
    if follow_up.get("refund_amount") is None:
        steps.append(
            f"Ask them to refund the {owed} you paid if it was not refunded automatically."
        )
    else:
        steps.append(f"Ask them to refund {owed} for the cancelled booking.")
    refs = []
    if follow_up.get("booking_id"):
        refs.append(f"booking ID {follow_up['booking_id']}")
    if follow_up.get("order_id"):
        refs.append(f"order reference {follow_up['order_id']}")
    if refs:
        steps.append(f"Give them {' and '.join(refs)} so they can find your payment.")
    return steps


def _cancel_user_message(session: Session, settlement: Dict[str, Any]) -> str:
    """What the buyer reads after a successful cancel, refund steps included."""
    business = _business_label(session)
    slot = reschedule_rules.current_slot(session)
    when = None
    if slot is not None:
        when = next(
            (
                text
                for tz_name in (session.business.timezone, session.buyer_timezone, "UTC")
                if (text := tz_context.range_display(slot.start, slot.end, tz_name))
            ),
            None,
        )
    lines = [
        f"Your booking with {business}"
        + (f" on {when}" if when else "")
        + " is cancelled."
    ]
    follow_up = settlement.get("refund_follow_up")
    if follow_up:
        paid = follow_up.get("captured_display") or "the amount you paid"
        if follow_up.get("refund_amount") is None:
            lines.append(
                f"{business} did not say whether the {paid} you paid will be refunded. "
                "To get it back:"
            )
        elif follow_up.get("refund_display"):
            lines.append(
                f"{business} refunded {follow_up['refund_display']} of the {paid} you paid. "
                "To get the rest back:"
            )
        else:
            lines.append(
                f"{business} did not refund the {paid} you paid. To get it back:"
            )
        lines.extend(f"{i}. {step}" for i, step in enumerate(follow_up["steps"], 1))
    elif settlement.get("captured_display"):
        refunded = settlement.get("refund_display") or settlement["captured_display"]
        lines.append(f"{business} is refunding the {refunded} you paid.")
    return "\n".join(lines)


def _merchant_site_url(session: Session) -> Optional[str]:
    """The merchant's origin, derived from the profile URL."""
    from urllib.parse import urlsplit

    parts = urlsplit(session.business.profile_url or "")
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def _merchant_cancel_handoff(session: Session, checkout: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Where the buyer can cancel once the checkout is closed to the agent.

    Money moved through the merchant's checkout, so only the merchant can
    cancel and refund together. Prefer live values from ``checkout``; fall back
    to what the session recorded when the checkout was completed.

    The order ``permalink_url`` is not offered: merchants commonly point it at a
    post-checkout thank-you page that renders only in the paying browser session,
    so later it is blank. The order id is kept as a reference instead.
    """
    order = checkout.get("order") if isinstance(checkout, dict) else None
    order = order if isinstance(order, dict) else {}
    urls = [
        ("booking", session.booking.booking_url),
        ("checkout", (checkout or {}).get("continue_url")),
        # Last resort: the merchant's own site. Worse than a deep link, but the
        # buyer must never be left with no way to reach the only party that can
        # cancel and refund.
        ("merchant_site", _merchant_site_url(session)),
    ]
    policy = ((session.live_service or {}).get("policies") or {}).get("cancellation")
    return {
        "cancel_urls": [
            {"type": kind, "url": url} for kind, url in urls if url
        ],
        "order_id": order.get("id") or session.payment.order_id,
        "merchant_links": session.ucp_checkout_links or [],
        "cancellation_policy": policy,
    }


async def _read_ucp_checkout_for_handoff(session: Session):
    """Best-effort read of the closed checkout, for handoff URLs only.

    Never raises: this runs on an error path, and a failed read must not
    replace the cancellation message the buyer needs with a transport error.
    """
    from ..payment_helpers import get_ucp_checkout_async

    try:
        return await get_ucp_checkout_async(session)
    except Exception:
        return None


async def _manage_url_for_cancel(
    session: Session,
    backend: Any,
    profile: Any,
    booking_id: str,
    *,
    credential: Any,
    credential_endpoint: Optional[str],
) -> Optional[str]:
    """The business's manage page, from the session or a fresh read.

    A read failure leaves the URL unknown. The caller then uses the
    credentialed cancel, which is the path the protocol still guarantees.
    """
    if session.booking.booking_url:
        return session.booking.booking_url
    get_booking = getattr(backend, "get_booking", None)
    if get_booking is None:
        return None
    try:
        fresh = await get_booking(
            profile,
            booking_id,
            credential=credential,
            credential_endpoint=credential_endpoint,
        )
    except Exception:
        return None
    url = _record_field(fresh, "booking_url")
    if isinstance(url, str) and url.strip():
        session.booking.booking_url = url
        return url
    return None


async def _cancel_booking_async(session: Session, booking_id: str, reason: Optional[str]):
    backend = resolve_backend()
    profile = await backend.resolve_profile(session.business.profile_url)
    endpoint = profile.rest_endpoint
    credential = credential_for_session_resource(
        session,
        endpoint=endpoint,
        resolved_business_endpoint=endpoint,
    )
    credential_endpoint = (
        session.booking.endpoint if credential is not None else None
    )

    booking_url = session.booking.booking_url
    # Set when checkout cancel reports the checkout already completed. §7.5
    # cancel_checkout only abandons an open checkout; the confirmed booking is
    # cancelled afterwards by POST /bookings/{id}/cancel (§5.3.5).
    closed_checkout = None
    if credential is None and session.booking.credential:
        # Present but unusable: expired, or issued by a different business.
        raise _CancelNeedsHuman(
            "credential_unusable",
            "The saved cancellation credential is expired or was issued by a "
            "different business. Cancel it from the manage page instead, the "
            "same page the confirmation email's Manage Booking link opens, and "
            "then contact the business to ask for a refund if one is due.",
            booking_url,
            handoff=_merchant_cancel_handoff(session, None),
        )

    if session.ucp_checkout_session_id and session.ucp_checkout_rest_base:
        from booking_agent.models.wire import BookingStatus
        from booking_agent.ucp_checkout import (
            UcpCheckoutHttpError,
            validate_ucp_checkout_canceled,
        )

        try:
            checkout = await cancel_ucp_checkout_async(
                session,
                credential=credential,
            )
        except UcpCheckoutHttpError as e:
            if e.status == 409:
                # Completed checkout is immutable. That is the correct UCP
                # answer (§7.5 cancel_checkout abandons an open checkout only).
                # The booking itself is still cancellable, and §5.3.5 is how.
                # Read it first so the buyer still has the order page if the
                # booking cancel cannot be authenticated.
                closed_checkout = await _read_ucp_checkout_for_handoff(session)
            elif e.status in _CANCEL_REJECTED_STATUSES:
                raise _cancel_rejected(session, e.status, booking_url, None) from e
            elif e.status != 404:
                raise
        else:
            booking_context = validate_ucp_checkout_canceled(
                checkout,
                expected_booking_id=booking_id,
            )
            if booking_context is not None:
                return (
                    SimpleNamespace(
                        id=booking_context["booking_id"],
                        status=BookingStatus(booking_context["booking_status"]),
                    ),
                    None,
                )

    manage_url = await _manage_url_for_cancel(
        session,
        backend,
        profile,
        booking_id,
        credential=credential,
        credential_endpoint=credential_endpoint,
    )
    if manage_url:
        # booking_url is the protocol's manage page for every business. When
        # the business supplied one, the buyer cancels there. The credentialed
        # POST /bookings/{id}/cancel runs only when that URL is absent.
        session.booking.booking_url = manage_url
        raise _CancelNeedsHuman(
            "cancel_at_business",
            "The business provided a page where this booking is managed. Do not "
            "cancel it here. Show the buyer this raw URL and tell them to cancel "
            f"it with the business on that page: {manage_url}",
            manage_url,
            handoff=_merchant_cancel_handoff(session, closed_checkout),
        )

    try:
        booking = await backend.cancel_booking(
            profile,
            booking_id,
            reason=reason,
            credential=credential,
            credential_endpoint=credential_endpoint,
            if_match=session.booking.revision,
        )
    except RevisionMismatch:
        # The booking moved on since we last read it, and the cancel was NOT
        # applied. Re-read to pick up the current state and revision, then retry
        # once against that. Retrying without re-reading -- or by dropping the
        # precondition to force it through -- would discard whatever concurrent
        # change caused the mismatch.
        fresh = await backend.get_booking(
            profile,
            booking_id,
            credential=credential,
            credential_endpoint=credential_endpoint,
        )
        session.booking.revision = getattr(fresh, "revision", None)
        if booking_status_value(fresh) in INACTIVE_BOOKING_STATUSES:
            # Someone else already finished it. Nothing to cancel; report what
            # it actually is rather than failing. ``confirmed`` is not finished:
            # that is the usual state of the booking this cancel is for, and a
            # revision bump (payment, a note, a clock) must not abandon it.
            return fresh, None
        booking = await backend.cancel_booking(
            profile,
            booking_id,
            reason=reason,
            credential=credential,
            credential_endpoint=credential_endpoint,
            if_match=session.booking.revision,
        )
    except PopAuthorizationError as e:
        # A 401 is never retried as the platform: under platform_key_pop there
        # is no platform identity to escalate to, which is what makes the
        # privilege-upgrade path USP forbids structurally impossible here.
        raise _CancelNeedsHuman(
            e.code,
            "The business rejected the cancellation credential. Cancel it from "
            "the manage page instead, the same page the confirmation email's "
            "Manage Booking link opens, and then contact the business to ask "
            "for a refund if one is due.",
            booking_url,
            handoff=_merchant_cancel_handoff(session, closed_checkout),
        ) from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code in _CANCEL_REJECTED_STATUSES:
            raise _cancel_rejected(
                session, e.response.status_code, booking_url, closed_checkout,
            ) from e
        raise

    return booking, _cancel_settlement(session, booking, closed_checkout)


_CANCEL_REJECTED_STATUSES = {401: "cancel_unauthorized", 403: "cancel_forbidden"}


def _cancel_rejected(
    session: Session,
    status: int,
    booking_url: Optional[str],
    checkout: Optional[Dict[str, Any]],
) -> _CancelNeedsHuman:
    """401 and 403 both mean the business will not take this cancel from us.

    Neither is retried: there is no platform identity to escalate to, so the
    buyer finishes on the business's page.
    """
    handoff = _merchant_cancel_handoff(session, checkout)
    handoff["http_status"] = status
    return _CancelNeedsHuman(
        _CANCEL_REJECTED_STATUSES[status],
        "The business did not accept the cancellation credential. "
        "Cancel it from the manage page instead, the same page the "
        "confirmation email's Manage Booking link opens, and then "
        "contact the business to ask for a refund if one is due.",
        booking_url,
        handoff=handoff,
    )


def _retire_booking_credential(session: Session) -> None:
    """Drop the credential, and the key if nothing else needs it.

    The key is per business endpoint while credentials are per booking, so it is
    only safe to destroy once no other session holds a live credential there --
    otherwise those bookings become permanently uncancellable.
    """
    endpoint = session.booking.endpoint
    clear_credential(session)
    if not endpoint:
        return
    store = load_store()
    if endpoint_still_in_use(store, endpoint, excluding_session_id=session.id):
        return
    PopKeyStore().release(endpoint, still_in_use=False)


# ---------------------------------------------------------------------------
# reschedule
# ---------------------------------------------------------------------------

reschedule_app = typer.Typer(
    help="Move a confirmed booking made on this device to another slot at the same price.",
)
app.add_typer(reschedule_app, name="reschedule")


class _RescheduleNeedsHuman(Exception):
    """The agent cannot move this booking: the buyer must use the business's page."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _manage_handoff(session: Session) -> Dict[str, Any]:
    urls = [
        ("booking", session.booking.booking_url),
        ("merchant_site", _merchant_site_url(session)),
    ]
    return {
        "booking_url": session.booking.booking_url,
        "manage_urls": [{"type": kind, "url": url} for kind, url in urls if url],
    }


def _emit_reschedule_handoff(session: Session, err: _RescheduleNeedsHuman) -> None:
    emit_error(
        session.flow_phase,
        code=err.code,
        message=err.message,
        data={"booking_id": session.booking.id, **_manage_handoff(session)},
    )


def _slot_view(slot: Optional[SlotRef]) -> Optional[Dict[str, Any]]:
    if slot is None:
        return None
    return {
        "slot_id": slot.slot_id,
        "start": slot.start,
        "end": slot.end,
        "price_amount": slot.price_amount,
    }


def _reschedule_dual(session: Session, slots: List[Optional[SlotRef]]) -> bool:
    return tz_context.zones_differ(
        session.business.timezone, session.buyer_timezone, [s.start for s in slots if s],
    )


def _zoned_slot_view(session: Session, slot: Optional[SlotRef], *, dual: bool) -> Optional[Dict[str, Any]]:
    view = _slot_view(slot)
    if view is None:
        return None
    view.update(tz_context.slot_displays(
        slot.start, slot.end, session.business.timezone, session.buyer_timezone, dual=dual,
    ))
    return view


def _slot_time_text(session: Session, slot: Optional[SlotRef], *, dual: bool) -> Optional[str]:
    if slot is None:
        return None
    business = tz_context.range_display(slot.start, slot.end, session.business.timezone)
    if business is None:
        return f"{slot.start} - {slot.end}"
    if dual:
        buyer = tz_context.range_display(slot.start, slot.end, session.buyer_timezone)
        return f"{business} ({buyer})" if buyer else business
    return business


def _reschedule_time_instruction(session: Session, dual: bool) -> str:
    if _valid_iana_timezone(session.business.timezone) is None:
        return f"Give each time exactly as shown. {_NO_BUSINESS_ZONE_TEXT}"
    if dual:
        return (
            "Give each time as the business's local time first with the user's local time "
            "in parentheses, exactly as shown."
        )
    return "Give each time as the business's local time, as shown."


def _reschedule_confirm_prompt(session: Session) -> str:
    old = reschedule_rules.find_slot(
        session.slots_presented, session.reschedule_original_slot_id or session.slot_id,
    )
    new = reschedule_rules.find_slot(session.reschedule_slots, session.reschedule_slot_id)
    dual = _reschedule_dual(session, [old, new])
    old_text = _slot_time_text(session, old, dual=dual) or "its current time"
    new_text = _slot_time_text(session, new, dual=dual) or str(session.reschedule_slot_id)
    return (
        "STOP. Ask the user to confirm moving this booking, in everyday words. Show the two "
        "times on separate labeled lines, never inside one 'from ... to ...' sentence "
        "(each time is already a range):\n"
        f"Current time: {old_text}\n"
        f"New time: {new_text}\n"
        f"{_reschedule_time_instruction(session, dual)} Run "
        "`booking-agent flow reschedule apply --user-confirmed` only after they agree. If they "
        "decline, run `booking-agent flow reschedule abort`."
    )


def _require_reschedulable(session: Session) -> None:
    if session.flow_phase != "CONFIRMED" or not session.booking.id:
        raise GateError(
            "reschedule_not_actionable",
            "Only a confirmed booking made on this device can be moved. Resume that "
            "booking's session first.",
            next_command="booking-agent flow sessions list",
        )
    if not session.service_id:
        raise GateError(
            "reschedule_not_actionable",
            "This booking has no service on record, so it cannot be moved here.",
        )
    if not is_actionable(session) or not reschedule_rules.slot_starts_in_future(
        reschedule_rules.current_slot(session)
    ):
        raise GateError(
            "reschedule_not_actionable",
            "This appointment has already started or passed, or its time is unknown, so "
            "it cannot be moved here.",
        )


def _require_reschedule_started(session: Session) -> None:
    if not reschedule_rules.rescheduling_in_progress(session):
        raise GateError(
            "reschedule_not_started",
            "No move is in progress for this booking.",
            next_command="booking-agent flow reschedule start",
        )


async def _reschedule_context(session: Session):
    """Backend, profile, and a usable booking credential, or a handoff."""
    backend = resolve_backend()
    profile = await backend.resolve_profile(session.business.profile_url)
    endpoint = profile.rest_endpoint
    credential = credential_for_session_resource(
        session,
        endpoint=endpoint,
        resolved_business_endpoint=endpoint,
    )
    if credential is None or credential.is_expired():
        raise _RescheduleNeedsHuman(
            "reschedule_at_business",
            "This booking cannot be moved from here because its saved booking credential "
            "is missing, expired, or belongs to a different business. Show the buyer the "
            "business's manage page as a raw URL and tell them to move it there.",
        )
    return backend, profile, credential, session.booking.endpoint


async def _reschedule_booking_async(session: Session, booking_id: str, chosen: SlotRef):
    """Returns ``(booking, outcome)`` where outcome is ``moved`` or ``inactive``."""
    backend, profile, credential, credential_endpoint = await _reschedule_context(session)
    auth = {"credential": credential, "credential_endpoint": credential_endpoint}
    try:
        try:
            booking = await backend.reschedule_booking(
                profile, booking_id, chosen.slot_id,
                if_match=session.booking.revision, **auth,
            )
            return booking, "moved"
        except RevisionMismatch:
            # The move was NOT applied. Re-read for the current revision and
            # retry once, so a concurrent change is reconciled, not overwritten.
            fresh = await backend.get_booking(profile, booking_id, **auth)
            session.booking.revision = getattr(fresh, "revision", None)
            if booking_status_value(fresh) in reschedule_rules.DEAD_BOOKING_STATUSES:
                return fresh, "inactive"
            if getattr(getattr(fresh, "slot", None), "id", None) == chosen.slot_id:
                return fresh, "moved"
            booking = await backend.reschedule_booking(
                profile, booking_id, chosen.slot_id,
                if_match=session.booking.revision, **auth,
            )
            return booking, "moved"
    except PopAuthorizationError as e:
        raise _RescheduleNeedsHuman(
            e.code,
            "The business rejected the booking credential. Show the buyer the "
            "business's manage page as a raw URL and tell them to move it there.",
        ) from e
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise _RescheduleNeedsHuman(
                "reschedule_unauthorized",
                "The business did not accept the booking credential. Show the buyer the "
                "business's manage page as a raw URL and tell them to move it there.",
            ) from e
        raise


@reschedule_app.command("start")
def reschedule_start() -> None:
    """Check that the active confirmed booking can be moved, then begin a move."""
    session = must_load()
    try:
        _require_reschedulable(session)
    except GateError as e:
        raise _handle_gate(session, e)

    async def _go():
        from ..catalog import live_service_summary

        backend, profile, _credential, _endpoint = await _reschedule_context(session)
        live = await backend.get_service(profile, session.service_id)
        return live_service_summary(live)

    try:
        live_summary = asyncio.run(_go())
    except _RescheduleNeedsHuman as e:
        _emit_reschedule_handoff(session, e)
        raise typer.Exit(code=1)
    except Exception as e:
        raise _emit_flow_http_error(
            session.flow_phase, e, default_code="reschedule_start_failed",
        )

    policy = (live_summary.get("policies") or {}).get("rescheduling")
    if not policy or not policy.get("allowed"):
        emit_error(
            session.flow_phase,
            code="reschedule_not_allowed",
            message=(
                "The business does not allow this booking to be moved here. If the buyer "
                "still wants to change it, show them the business's manage page as a raw URL."
            ),
            data={"booking_id": session.booking.id, **_manage_handoff(session)},
        )
        raise typer.Exit(code=1)
    fee = reschedule_rules.minor_units(policy.get("fee"))
    if fee:
        emit_error(
            session.flow_phase,
            code="reschedule_fee_unsupported",
            message=(
                f"The business charges a fee to move this booking ({fee} in minor units), "
                "and fees cannot be collected here. Show the buyer the business's manage "
                "page as a raw URL if they still want to move it."
            ),
            data={"booking_id": session.booking.id, **_manage_handoff(session)},
        )
        raise typer.Exit(code=1)

    reschedule_rules.clear_reschedule(session)
    session.reschedule_original_slot_id = session.slot_id
    save_session(session)
    emit(_agent_out(
        session,
        prompt=(
            "This booking can be moved to another open slot at the same price. If the user "
            "has not said when they want it instead, ask them and STOP. Then run "
            "`booking-agent flow reschedule availability` with --target-date or --from/--to "
            "matching their answer."
        ),
        next_command="booking-agent flow reschedule availability",
        data={
            "booking_id": session.booking.id,
            "current_slot": _zoned_slot_view(
                session, reschedule_rules.current_slot(session),
                dual=_reschedule_dual(session, [reschedule_rules.current_slot(session)]),
            ),
            "rescheduling_policy": policy,
        },
    ))


@reschedule_app.command("availability")
def reschedule_availability(
    start: Optional[str] = typer.Option(None, "--from", help="ISO8601 start (default: tomorrow)"),
    end: Optional[str] = typer.Option(None, "--to", help="ISO8601 end (default: +14 days)"),
    target_date: Optional[str] = typer.Option(
        None, "--target-date", help="Single calendar day to query (YYYY-MM-DD).",
    ),
) -> None:
    """List open slots the booking can move to, without changing the booking."""
    session = must_load()
    try:
        _require_reschedulable(session)
        _require_reschedule_started(session)
    except GateError as e:
        raise _handle_gate(session, e)

    try:
        window_from, window_to = _resolve_window(
            start, end, target_date, session.business.timezone,
        )
    except ValueError:
        emit_error(
            session.flow_phase,
            code="invalid_target_date",
            message=f"--target-date must be an ISO date (YYYY-MM-DD); got {target_date!r}.",
        )
        raise typer.Exit(code=1)

    try:
        live_summary, slots, calendar_filtered, slots_filtered, response_zones = asyncio.run(
            _query_slots_async(session, session.service_id, window_from, window_to)
        )
    except Exception as e:
        emit_error(session.flow_phase, code="availability_failed", message=str(e))
        raise typer.Exit(code=1)

    from ..flow_timings import apply_pending_timings

    apply_pending_timings(session)
    adopted = _adopt_response_timezone(session, response_zones)

    candidates = [
        SlotRef(
            slot_id=s.id,
            start=s.start,
            end=s.end,
            duration=s.duration,
            price_amount=reschedule_rules.candidate_price_minor(
                s.pricing.amount if s.pricing and s.pricing.amount is not None else None,
                live_summary,
            ),
        )
        for s in slots
        if s.id != session.slot_id
    ]
    session.reschedule_slots = candidates
    session.reschedule_slot_id = None
    session.reschedule_user_confirmed = False
    _ensure_buyer_timezone(session)
    save_session(session)

    current = reschedule_rules.current_slot(session)
    dual = _reschedule_dual(session, [current, *candidates])
    rows = []
    for slot in candidates:
        refusal = reschedule_rules.price_refusal(session, slot)
        rows.append({
            **_zoned_slot_view(session, slot, dual=dual),
            "selectable": refusal is None,
            "not_selectable_reason": refusal[0] if refusal else None,
        })
    selectable = sum(1 for row in rows if row["selectable"])
    zone_data: Dict[str, Any] = {"business_timezone": session.business.timezone, **adopted}
    zone_text = "Show each slot's business_start_display (the business's local time). "
    if _valid_iana_timezone(session.business.timezone) is None:
        zone_text = f"Show each slot's business_start_display. {_NO_BUSINESS_ZONE_TEXT} "
    elif dual:
        zone_data["buyer_timezone"] = session.buyer_timezone
        zone_data["timezone_notice"] = tz_context.timezone_notice(
            session.business.timezone, session.buyer_timezone,
        )
        zone_text = (
            "Post data.timezone_notice once, above the list. Show each slot as its "
            "business_start_display first, with its buyer_start_display in parentheses. "
        )
    if selectable:
        prompt = (
            f"{selectable} slot(s) can replace the current booking time. Present only the "
            "selectable ones as a numbered or bulleted list (never a markdown table) and WAIT "
            f"for the user to pick. {zone_text}Then call "
            "`booking-agent flow reschedule select --slot-id <id> --user-confirmed`."
        )
        next_cmd = "booking-agent flow reschedule select --slot-id <id> --user-confirmed"
    else:
        prompt = (
            "No open slot at the same price in this window. Ask the user for another time "
            "and query again, or run `booking-agent flow reschedule abort` to keep the booking."
        )
        next_cmd = "booking-agent flow reschedule availability"
    emit(_agent_out(
        session,
        awaiting_user="reschedule_slot_choice" if selectable else None,
        prompt=prompt,
        next_command=next_cmd,
        data={
            "window": {"from": window_from, "to": window_to},
            "current_slot": _zoned_slot_view(session, current, dual=dual),
            "slots": rows,
            "calendar_filtered": calendar_filtered,
            "slots_filtered": slots_filtered,
            **zone_data,
        },
    ))


@reschedule_app.command("select")
def reschedule_select(
    slot_id: str = typer.Option(..., "--slot-id"),
    user_confirmed: bool = typer.Option(
        False, "--user-confirmed",
        help="Required. Indicates the human, not the agent, picked this slot.",
    ),
) -> None:
    """Record the user's chosen new slot. The booking itself is not changed yet."""
    session = must_load()
    try:
        _require_reschedulable(session)
        _require_reschedule_started(session)
    except GateError as e:
        raise _handle_gate(session, e)

    if not session.reschedule_slots:
        emit_error(
            session.flow_phase,
            code="no_reschedule_slots",
            message="No candidate slots yet. Query availability for the move first.",
            next_command="booking-agent flow reschedule availability",
        )
        raise typer.Exit(code=1)
    if not user_confirmed:
        emit_error(
            session.flow_phase,
            code="slot_not_user_confirmed",
            message="Pass --user-confirmed only after the user picks a slot. "
                    "Auto-picking on the user's behalf is forbidden.",
        )
        raise typer.Exit(code=1)
    if slot_id == session.slot_id:
        emit_error(
            session.flow_phase,
            code="reschedule_same_slot",
            message="That is the slot the booking already has. Ask the user for a different one.",
        )
        raise typer.Exit(code=1)
    chosen = reschedule_rules.find_slot(session.reschedule_slots, slot_id)
    if chosen is None:
        emit_error(
            session.flow_phase,
            code="unknown_slot_id",
            message=f"Slot '{slot_id}' was not offered for this move. Query availability again.",
            next_command="booking-agent flow reschedule availability",
        )
        raise typer.Exit(code=1)
    refusal = reschedule_rules.price_refusal(session, chosen)
    if refusal:
        emit_error(
            session.flow_phase,
            code=refusal[0],
            message=refusal[1],
            next_command="booking-agent flow reschedule availability",
        )
        raise typer.Exit(code=1)

    session.reschedule_slot_id = slot_id
    session.reschedule_user_confirmed = True
    save_session(session)
    select_dual = _reschedule_dual(session, [reschedule_rules.current_slot(session), chosen])
    emit(_agent_out(
        session,
        awaiting_user="reschedule_confirm",
        prompt=_reschedule_confirm_prompt(session),
        next_command="booking-agent flow reschedule apply --user-confirmed",
        data={
            "booking_id": session.booking.id,
            "current_slot": _zoned_slot_view(
                session, reschedule_rules.current_slot(session), dual=select_dual,
            ),
            "new_slot": _zoned_slot_view(session, chosen, dual=select_dual),
        },
    ))


@reschedule_app.command("apply")
def reschedule_apply(
    user_confirmed: bool = typer.Option(
        False, "--user-confirmed",
        help="Required. The user agreed to move the booking to the selected slot.",
    ),
) -> None:
    """Ask the business to move the booking to the selected slot."""
    session = must_load()
    try:
        _require_reschedulable(session)
        _require_reschedule_started(session)
    except GateError as e:
        raise _handle_gate(session, e)

    chosen = reschedule_rules.find_slot(session.reschedule_slots, session.reschedule_slot_id)
    if chosen is None or not session.reschedule_user_confirmed:
        emit_error(
            session.flow_phase,
            code="reschedule_slot_not_selected",
            message="The user has not picked a new slot for this booking yet.",
            next_command="booking-agent flow reschedule select --slot-id <id> --user-confirmed",
        )
        raise typer.Exit(code=1)
    if not user_confirmed:
        emit_error(
            session.flow_phase,
            code="reschedule_not_user_confirmed",
            message="Pass --user-confirmed only after the user agrees to move the booking.",
        )
        raise typer.Exit(code=1)
    refusal = reschedule_rules.price_refusal(session, chosen)
    if refusal:
        emit_error(
            session.flow_phase,
            code=refusal[0],
            message=refusal[1],
            next_command="booking-agent flow reschedule availability",
        )
        raise typer.Exit(code=1)

    bid = session.booking.id
    try:
        booking, outcome = asyncio.run(_reschedule_booking_async(session, bid, chosen))
    except _RescheduleNeedsHuman as e:
        save_session(session)
        _emit_reschedule_handoff(session, e)
        raise typer.Exit(code=1)
    except BookingServerError as e:
        save_session(session)
        emit_error(
            session.flow_phase,
            code="reschedule_failed",
            message=f"The business refused the move: [{e.code}] {e.server_message}",
            next_command="booking-agent flow reschedule availability",
            data={"booking_id": bid, "server_code": e.code},
        )
        raise typer.Exit(code=1)
    except Exception as e:
        save_session(session)
        emit_error(
            session.flow_phase,
            code="reschedule_failed",
            message=str(e),
            data={"booking_id": bid},
        )
        raise typer.Exit(code=1)

    status_val = booking_status_value(booking)
    if outcome == "inactive":
        reschedule_rules.clear_reschedule(session)
        save_session(session)
        emit_error(
            session.flow_phase,
            code="reschedule_booking_not_active",
            message=f"The booking is now {status_val}, so it was not moved.",
            data={"booking_id": bid, "status": status_val},
        )
        raise typer.Exit(code=1)

    previous_ref = reschedule_rules.find_slot(
        session.slots_presented, session.reschedule_original_slot_id,
    )
    new_ref = reschedule_rules.apply_rescheduled_booking(session, booking, chosen)
    save_session(session)

    update_calendar = (
        session.calendar_mode == CALENDAR_MODE_EVENTS and session.calendar_state != "skipped"
    )
    apply_dual = _reschedule_dual(session, [previous_ref, new_ref])
    previous = _zoned_slot_view(session, previous_ref, dual=apply_dual)
    prompt = (
        f"Booking {bid} moved to {_slot_time_text(session, new_ref, dual=apply_dual)}. Tell "
        f"the user in everyday words. {_reschedule_time_instruction(session, apply_dual)}"
    )
    if status_val != "confirmed":
        prompt += (
            f" The business reports the booking as '{status_val}'; tell the user it may need "
            "the business to confirm the new time."
        )
    if update_calendar:
        prompt += (
            " Then run next_command to move their calendar event. If that fails, the "
            "appointment itself is still moved."
        )
    emit(_agent_out(
        session,
        prompt=prompt,
        next_command="booking-agent flow calendar update-event" if update_calendar else None,
        data={
            "booking_id": bid,
            "status": status_val,
            "previous_slot": previous,
            "new_slot": _zoned_slot_view(session, new_ref, dual=apply_dual),
        },
    ))


@reschedule_app.command("abort")
def reschedule_abort() -> None:
    """Drop an in-progress move. The booking is left as it is."""
    session = must_load()
    was_in_progress = reschedule_rules.rescheduling_in_progress(session)
    reschedule_rules.clear_reschedule(session)
    save_session(session)
    emit(_agent_out(
        session,
        prompt=(
            "The move was dropped; the booking keeps its current time."
            if was_in_progress
            else "No move was in progress; the booking is unchanged."
        ),
        data={"booking_id": session.booking.id, "aborted": was_in_progress},
    ))


# ---------------------------------------------------------------------------
# validate-skill-compliance
# ---------------------------------------------------------------------------


@app.command("validate-skill-compliance")
def validate_skill_compliance() -> None:
    """Exits 0 iff every hard gate has been satisfied, otherwise exits 1.

    Per PLAN §6.2: requires phase ∈ {BOOKED, PAYMENT_PENDING, CONFIRMED} AND
    calendar_gate_completed AND slot_user_confirmed AND
    buyer_source == "link-cli user-info retrieve".
    """
    session = must_load()
    _apply_skill_compliance(session)
    emit(_agent_out(
        session,
        prompt="All hard gates satisfied. Booking is compliant with the skill.",
        data={"compliant": True},
    ))


# ---------------------------------------------------------------------------
# support-report (spec #307)
# ---------------------------------------------------------------------------

from ..support_report import support_app  # noqa: E402

app.add_typer(support_app, name="support-report")
