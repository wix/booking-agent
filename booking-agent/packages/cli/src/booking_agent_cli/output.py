"""Agent-facing JSON output helpers.

Every flow command emits one JSON object with the following shape::

    {
      "phase": "<flow_phase>",
      "awaiting_user": "<string|null>",
      "user_message": "<string|null>",
      "prompt": "<string|null>",
      "next_command": "<string|null>",
      "forbidden": ["..."],
      "data": { ... arbitrary command-specific payload ... }
    }

``user_message`` is optional end-user text. For Link spend approval it is **only** the
approval URL (no labels or booking context). When set, ``emit`` writes that text as the
**first stdout line** (before the JSON). Agents must show ``user_message`` to the user
immediately when present (as the raw URL only — never markdown ``[label](url)``). For spend
approval, ``awaiting_user`` is set to
``link_spend_approval`` so agents **stop** and surface the URL before polling or other tools.
Operational context stays on stderr (agents must not relay stderr to the user).

This is what skills/usp-platform-link/SKILL.md tells the agent to read on
every iteration of the loop.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentOutput:
    phase: str
    awaiting_user: Optional[str] = None
    user_message: Optional[str] = None
    prompt: Optional[str] = None
    next_command: Optional[str] = None
    forbidden: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    # Set only when this failure offers the consented support report (spec #307).
    support_report_offered: Optional[bool] = None


def _fill_correlation_id(out: AgentOutput) -> AgentOutput:
    if out.correlation_id:
        return out
    try:
        from booking_agent.observability import current

        cid = current()
    except Exception:
        cid = None
    if cid:
        out.correlation_id = cid
    return out


def emit(out: AgentOutput, *, stream=None) -> None:
    out = _fill_correlation_id(out)
    stream = stream if stream is not None else sys.stdout
    if out.user_message:
        stream.write(out.user_message)
        stream.write("\n")
        stream.flush()
    payload = asdict(out)
    if payload.get("support_report_offered") is None:
        payload.pop("support_report_offered", None)
    stream.write(json.dumps(payload, indent=2, default=str))
    stream.write("\n")
    stream.flush()


def write_spend_approval_notice(approval_url: Optional[str]) -> None:
    """Agent-only spend-approval context on stderr (not for end users).

    The approval URL is emitted once on stdout via ``user_message``; stderr must not
    repeat it or agents see duplicate links in shell output.
    """
    if approval_url:
        sys.stderr.write(
            "booking-agent: spend request created; show user_message from stdout to the user\n"
        )
        sys.stderr.flush()


# Machine-readable gate: agents must STOP and surface ``user_message`` before any
# other tool call or background-shell wait (user approves in Link, not in chat).
AWAITING_LINK_SPEND_APPROVAL = "link_spend_approval"
AWAITING_LINK_AUTH_APPROVAL = "link_auth_approval"

# A foreground poll blocks the turn, so clients that deliver text only when the turn
# ends (or agents that call the tool before writing) never show the approval URL.
PAY_POLL_BACKGROUND_INSTRUCTION = (
    "Start `booking-agent flow pay poll` in the background (run_in_background) in this same "
    "turn, and post the raw URL to the user right away; do not block the turn on the "
    "poll before the user has seen the URL. Do not wait for a chat reply. When the poll "
    "exits, tell the user the outcome and do not repeat the URL."
)


def spend_approval_user_message(approval_url: Optional[str]) -> Optional[str]:
    """Bare approval URL for stdout ``user_message`` (shown to the end user)."""
    return approval_url or None


def spend_approval_prompt(approval_url: Optional[str], *, polling: bool = False) -> str:
    if approval_url:
        poll_note = (
            " This command is polling for approval in the background."
            if polling
            else f" {PAY_POLL_BACKGROUND_INSTRUCTION}"
        )
        return (
            "STOP. Post user_message to the user as the raw URL (never markdown [label](url)) "
            f"in this reply.{poll_note} "
            "The user approves in the Link app — do not ask them to type approval in chat. "
            "Do not relay stderr to the user."
        )
    return "Spend request created; approval URL unavailable from link-cli."


def spend_approval_surface(
    approval_url: Optional[str],
    *,
    polling: bool = False,
    then_complete: bool = False,
) -> tuple[Optional[str], Optional[str], str]:
    """Return (awaiting_user, user_message, prompt) for Link spend approval."""
    user_message = spend_approval_user_message(approval_url)
    if then_complete:
        awaiting_user = None
        prompt = (
            "Scripts/CI only: this command blocks until checkout finishes. Interactive "
            "agents should use default pay request (immediate return) instead. "
            "Post user_message to the user as the raw URL (never markdown [label](url)) before "
            "the blocking poll "
            "starts (stdout is flushed first). Do not run pay ucp-complete or pay checkout "
            "in a separate step."
        )
    else:
        awaiting_user = AWAITING_LINK_SPEND_APPROVAL if approval_url else None
        prompt = spend_approval_prompt(approval_url, polling=polling)
    return awaiting_user, user_message, prompt


def emit_error(
    phase: str,
    *,
    code: str,
    message: str,
    next_command: Optional[str] = None,
    forbidden: Optional[List[str]] = None,
    data: Optional[Dict[str, Any]] = None,
    user_message: Optional[str] = None,
    stream=None,
) -> None:
    offered = False
    try:
        from .activity_log import persist_error_if_session

        offered = bool(persist_error_if_session(
            code, message, next_command=next_command, phase=phase, exc=sys.exc_info()[1],
        ))
    except Exception:
        offered = False
    payload: Dict[str, Any] = {"error": {"code": code, "message": message}}
    if data:
        payload.update(data)
    prompt = f"[{code}] {message}"
    if offered:
        from .support_report import SUPPORT_REPORT_COMMAND

        next_command = SUPPORT_REPORT_COMMAND
        prompt += (
            " This could not be recovered, so a note to the booking-assistant team is "
            "offered: post user_message (if any) about the booking first, then run "
            "next_command in the same turn. It asks the user whether to send the note."
        )
    out = AgentOutput(
        phase=phase,
        awaiting_user=None,
        user_message=user_message,
        prompt=prompt,
        next_command=next_command,
        forbidden=forbidden or [],
        data=payload,
        support_report_offered=True if offered else None,
    )
    emit(out, stream=stream)
