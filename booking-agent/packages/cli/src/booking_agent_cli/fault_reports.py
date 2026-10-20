"""Automatic reports of faults the user never saw.

A fault qualifies when the user never saw it (the command did not end in an error the
user is told about), the user is not its subject (a business's profile or catalog, the
registry, or the client's own code), and the report can be built only from the
allowlist in ``FAULT_REPORT_SCHEMA``. Such a report goes to the support ingest without
a consent prompt; everything else stays on the consented support-report path.

Reports are queued in ``fault-queue.jsonl`` and sent after the command's stdout is
written, within a one-second budget, silently. ``BOOKING_AGENT_FAULT_REPORTS=0``
turns this off.
"""
from __future__ import annotations

import json
import platform
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from booking_agent import faults, settings

from .fault_report_schema import FAULT_REPORT_SCHEMA
from .support_bundle_schema import validate_bundle

QUEUE_NAME = "fault-queue.jsonl"
SENT_NAME = "fault-sent.json"
MAX_QUEUE = 50
MAX_VIOLATIONS = 20
DEDUPE_SECONDS = 24 * 3600
SEND_BUDGET_S = 1.0
_MIN_ATTEMPT_S = 0.05

_visible_error = False
_violations: List[Dict[str, Any]] = []


def enabled() -> bool:
    return settings.get_bool("BOOKING_AGENT_FAULT_REPORTS")


def _contract_codes() -> frozenset:
    from .support_report import AGENT_CONTRACT_CODES

    return AGENT_CONTRACT_CODES | {"skill_compliance_failed"}


def _command() -> Optional[str]:
    from .support_report import sanitize_command

    try:
        return sanitize_command("booking-agent " + shlex.join(sys.argv[1:]))
    except Exception:
        return None


def note_error(code: str) -> None:
    """Called for every error the CLI emits. Never raises."""
    global _visible_error
    try:
        if code in _contract_codes():
            if len(_violations) < MAX_VIOLATIONS:
                _violations.append({
                    "fault_class": "agent_contract_violation",
                    "step": "cli",
                    "error_code": code,
                    "command": _command(),
                })
        else:
            _visible_error = True
    except Exception:
        pass


def _env() -> Dict[str, Any]:
    from . import __version__
    from .skill_bundle import SKILL_VERSION

    system = platform.system()
    return {
        "cli_version": __version__,
        "skill_version": SKILL_VERSION,
        "os_family": system if system in ("Darwin", "Linux", "Windows") else "other",
    }


def _fingerprint(report: Dict[str, Any]) -> str:
    from .gates import digest

    subject = report.get("subject") or {}
    return digest([
        report.get("fault_class"), report.get("step"), report.get("exception_type"),
        report.get("message_template"), report.get("reason_code"), report.get("error_code"),
        subject.get("host"), subject.get("logger"),
    ])


_SUBJECT_KEYS = ("host", "path", "business_id", "service_ids", "http_status", "logger")


def project(raw: Dict[str, Any], *, now: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The report for one recorded fault, built only from allowlisted keys; None if invalid."""
    from datetime import datetime, timezone

    subject_in = raw.get("subject") if isinstance(raw.get("subject"), dict) else {}
    subject = {k: subject_in[k] for k in _SUBJECT_KEYS if subject_in.get(k) is not None}
    report: Dict[str, Any] = {
        "kind": "fault_report",
        "report_version": 1,
        "created_at": now or datetime.now(timezone.utc).isoformat(),
        "fault_class": raw.get("fault_class"),
        "step": raw.get("step"),
        "env": _env(),
    }
    for key in ("exception_type", "message_template", "reason_code", "error_code", "command"):
        if raw.get(key) is not None:
            report[key] = raw[key]
    if subject:
        report["subject"] = subject
    report["fingerprint"] = _fingerprint(report)
    return report if not validate_bundle(report, FAULT_REPORT_SCHEMA) else None


def collect() -> List[Dict[str, Any]]:
    """This command's reports. Clears what was recorded."""
    global _visible_error
    recorded = faults.drain()
    if _visible_error:
        # The user was told something went wrong: that stays on the consented path.
        recorded = []
    recorded.extend(_violations)
    _violations.clear()
    _visible_error = False
    return [r for r in (project(raw) for raw in recorded) if r is not None]


def _home() -> Path:
    return settings.booking_agent_home()


def _read_queue(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("fingerprint"), str):
            out.append(item)
    return out


def _read_sent(path: Path, now: float) -> Dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: v for k, v in raw.items()
        if isinstance(v, (int, float)) and now - v < DEDUPE_SECONDS
    }


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _write_queue(path: Path, queue: List[Dict[str, Any]]) -> None:
    if not queue:
        path.unlink(missing_ok=True)
        return
    _write(path, "".join(json.dumps(item, sort_keys=True) + "\n" for item in queue))


def enqueue(reports: List[Dict[str, Any]], *, now: Optional[float] = None) -> None:
    """Add reports not already queued or sent in the last day; keep the queue bounded."""
    if not reports:
        return
    now = time.time() if now is None else now
    home = _home()
    queue = _read_queue(home / QUEUE_NAME)
    sent = _read_sent(home / SENT_NAME, now)
    known = {item["fingerprint"] for item in queue} | set(sent)
    for report in reports:
        if report["fingerprint"] in known:
            continue
        known.add(report["fingerprint"])
        queue.append(report)
    _write_queue(home / QUEUE_NAME, queue[-MAX_QUEUE:])


def send_queued(
    *,
    transport: Optional[httpx.BaseTransport] = None,
    budget_s: float = SEND_BUDGET_S,
    now: Optional[float] = None,
) -> None:
    """Send queued reports until the budget runs out; keep what was not accepted."""
    from .support_report import DEFAULT_SUPPORT_INGEST_URL, _post_report

    home = _home()
    queue = _read_queue(home / QUEUE_NAME)
    if not queue:
        return
    now = time.time() if now is None else now
    sent = _read_sent(home / SENT_NAME, now)
    deadline = time.monotonic() + budget_s
    remaining: List[Dict[str, Any]] = []
    for index, report in enumerate(queue):
        left = deadline - time.monotonic()
        if left < _MIN_ATTEMPT_S:
            remaining.extend(queue[index:])
            break
        body = json.dumps(report, sort_keys=True).encode("utf-8")
        try:
            failure = _post_report(DEFAULT_SUPPORT_INGEST_URL, body, transport=transport, timeout=left)
        except Exception:
            failure = "network"
        if failure is None:
            sent[report["fingerprint"]] = now
        elif failure == "schema":
            continue
        else:
            remaining.extend(queue[index:])
            break
    _write_queue(home / QUEUE_NAME, remaining)
    _write(home / SENT_NAME, json.dumps(sent, sort_keys=True))


def finish_command(*, transport: Optional[httpx.BaseTransport] = None) -> None:
    """After stdout is flushed: queue this command's reports and send the queue. Silent."""
    try:
        reports = collect()
        if not enabled():
            return
        enqueue(reports)
        send_queued(transport=transport)
    except Exception:
        pass
