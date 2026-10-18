"""Flow state machine (Section 6 of PLAN).

The engine reads + writes ``.usp-session.json`` via session.py. Gate
helpers raise ``GateError`` so command handlers can emit a uniform
agent-facing JSON error and exit 1.
"""
from __future__ import annotations

from dataclasses import dataclass

from .session import BUYER_SOURCE_LINKCLI, Buyer, Session


class GateError(Exception):
    """Raised when a hard gate has not been satisfied."""

    def __init__(self, code: str, message: str, *, next_command: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.next_command = next_command


@dataclass(frozen=True)
class TransitionRule:
    """Allowed phase progression check (advisory; used by validate)."""

    name: str


def require_phase(session: Session, *allowed: str, code: str, next_command: str | None = None) -> None:
    if session.flow_phase not in allowed:
        raise GateError(
            code,
            f"Wrong phase '{session.flow_phase}'. Expected one of: {', '.join(allowed)}.",
            next_command=next_command,
        )


def require_business(session: Session) -> None:
    if not session.business.profile_url:
        raise GateError(
            "no_business",
            "No business selected. Run `booking-agent flow business use --profile-url ...` "
            "or `booking-agent flow discover`.",
            next_command="booking-agent flow discover",
        )


def require_calendar_gate(session: Session) -> None:
    if not session.calendar_gate_completed:
        raise GateError(
            "calendar_gate_not_completed",
            "Calendar gate not completed. Ask the user about calendar conflict "
            "checking, then run `booking-agent flow calendar ask|skip|connect`.",
            next_command="booking-agent flow calendar ask",
        )


def require_link_auth_gate(session: Session) -> None:
    if not session.link_auth_gate_completed:
        raise GateError(
            "link_auth_gate_not_completed",
            "Link wallet authentication not completed. Run "
            "`booking-agent flow link-auth connect`, post the sign-in link, and start "
            "`booking-agent flow link-auth poll` in the background in the same turn.",
            next_command="booking-agent flow link-auth connect",
        )


def require_checkout_prereq(session: Session) -> None:
    if session.checkout_prereq_completed:
        return
    if getattr(session, "payment_route", None) in ("free", "at_service"):
        return
    raise GateError(
        "checkout_prereq_not_completed",
        "Checkout prerequisites were not completed. Run the checkout-mode "
        "orchestrator before the calendar gate.",
        next_command="booking-agent flow use-ucp-checkout-mode",
    )


def require_slot_selected(session: Session) -> None:
    if not session.slot_id or not session.slot_user_confirmed:
        raise GateError(
            "slot_not_selected",
            "No user-confirmed slot. Present `slots_presented` to the user, "
            "wait for their pick, then call `booking-agent flow slots select`.",
            next_command="booking-agent flow slots list",
        )


def require_buyer_from_link_cli(session: Session) -> None:
    if session.buyer_source != BUYER_SOURCE_LINKCLI:
        raise GateError(
            "buyer_not_from_link_cli",
            "Buyer must come from `link-cli user-info retrieve`. Placeholders, "
            "hand-entered values, and other sources are rejected.",
            next_command="booking-agent flow buyer resolve",
        )
    email = session.buyer.email or ""
    if email and not email.lower().endswith("@example.com"):
        return
    from .link_cli import (
        LinkCliUserInfoError,
        buyer_fields_from_user_info,
        retrieve_user_info,
    )

    try:
        payload = retrieve_user_info()
    except LinkCliUserInfoError as e:
        raise GateError(
            e.code,
            e.message,
            next_command="booking-agent flow buyer resolve",
        ) from e
    name, email, phone = buyer_fields_from_user_info(payload)
    if not email or email.lower().endswith("@example.com"):
        raise GateError(
            "buyer_email_invalid",
            "Buyer email is missing or uses an @example.com placeholder.",
            next_command="booking-agent flow buyer resolve",
        )
    session.buyer = Buyer(name=name, email=email, phone=phone)


def transition(session: Session, *, to: str) -> None:
    """In-place phase update with the previous phase implicit."""
    session.flow_phase = to


def recommend_next(session: Session) -> str | None:
    """Best-effort suggestion for the agent's next command."""
    phase = session.flow_phase
    pending = session.pending_booking_attempt
    if isinstance(pending, dict) and isinstance(pending.get("retry_command"), str):
        return pending["retry_command"]
    if phase == "CONFIRMED" and session.reschedule_original_slot_id is not None:
        if session.reschedule_slot_id:
            return "booking-agent flow reschedule apply --user-confirmed"
        if session.reschedule_slots:
            return "booking-agent flow reschedule select --slot-id <id> --user-confirmed"
        return "booking-agent flow reschedule availability"
    if phase == "BUSINESS_SELECTED":
        return "booking-agent flow use-ucp-checkout-mode"
    if phase == "BOOKED":
        return (
            "booking-agent flow pay request --payment-method-id <id>"
        )
    if phase == "PAYMENT_PENDING":
        sr = session.payment.spend_request_id
        if sr:
            return f"booking-agent flow pay ucp-complete --spend-request-id {sr}"
        return "booking-agent flow pay ucp-complete"
    table = {
        "INIT": "booking-agent flow prereq check",
        "PREREQ_OK": "booking-agent flow discover",
        "CALENDAR_GATE": "booking-agent flow availability",
        "AVAILABILITY_QUERIED": "booking-agent flow slots list",
        "SLOTS_PRESENTED": "booking-agent flow slots select --slot-id <id> --user-confirmed",
        "SLOT_SELECTED": "booking-agent flow link-auth connect",
        "BUYER_RESOLVED": "booking-agent flow book",
        "CONFIRMED": None,
        "FAILED": None,
    }
    return table.get(phase)
