#!/usr/bin/env python3
"""Poll session store and tear down offline demo on CONFIRMED or FAILED."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from support.mock.offline_demo_paths import booking_agent_home, marker_path
from support.mock.offline_demo_teardown import read_marker, teardown_offline_demo

TERMINAL_PHASES = frozenset({"CONFIRMED", "FAILED"})
POLL_INTERVAL_SEC = 1.5


def _session_store_path() -> Path:
    home = booking_agent_home()
    override = os.environ.get("BOOKING_AGENT_SESSION_DIR", "").strip()
    if override:
        return Path(override).expanduser() / ".usp-session.json"
    return home / ".usp-session.json"


def _parse_started_at(raw: Any) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_store() -> Dict[str, Any]:
    path = _session_store_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _active_session(store: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[datetime]]:
    active_id = store.get("active_session_id")
    sessions = store.get("sessions")
    if not active_id or not isinstance(sessions, list):
        return None, None, None
    for item in sessions:
        if not isinstance(item, dict):
            continue
        if item.get("id") == active_id:
            created = _parse_started_at(item.get("created_at"))
            return str(active_id), str(item.get("flow_phase") or ""), created
    return str(active_id), None, None


def _should_ignore_session(
    created: Optional[datetime],
    started_at: Optional[datetime],
) -> bool:
    return bool(started_at and created and created < started_at)


def check_session_for_teardown(
    *,
    last_phase_by_session: Dict[str, str],
    marker: Dict[str, Any],
    started_at: Optional[datetime],
) -> bool:
    """Poll session store once; tear down on terminal transition. Returns True if torn down."""
    store = _load_store()
    session_id, phase, created = _active_session(store)

    if session_id and phase and not _should_ignore_session(created, started_at):
        prev_phase = last_phase_by_session.get(session_id)
        if phase in TERMINAL_PHASES and prev_phase and prev_phase not in TERMINAL_PHASES:
            teardown_offline_demo(reason=f"phase={phase}", marker=marker)
            return True
        if prev_phase != phase:
            last_phase_by_session[session_id] = phase
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline demo session watcher")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=POLL_INTERVAL_SEC,
        help="Seconds between session store polls",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll iteration (for tests)",
    )
    args = parser.parse_args()

    marker = read_marker()
    if marker is None:
        print("No offline-demo.active marker; exiting.", file=sys.stderr)
        return 0

    started_at = _parse_started_at(marker.get("started_at"))
    last_phase_by_session: Dict[str, str] = {}

    while True:
        if not marker_path().exists():
            print("Marker removed; watcher exiting.", flush=True)
            return 0

        if check_session_for_teardown(
            last_phase_by_session=last_phase_by_session,
            marker=marker,
            started_at=started_at,
        ):
            return 0

        if args.once:
            return 0

        time.sleep(max(0.2, float(args.poll_interval)))


if __name__ == "__main__":
    raise SystemExit(main())
