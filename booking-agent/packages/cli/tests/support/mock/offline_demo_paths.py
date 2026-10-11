"""Paths and marker filenames for the offline UCP demo stack."""
from __future__ import annotations

import os
from pathlib import Path

MARKER_FILENAME = "offline-demo.active"
STACK_PID_FILENAME = "offline-stack.pid"
STACK_PORT_FILENAME = "offline-stack.port"
WATCHER_PID_FILENAME = "offline-watcher.pid"
LOG_FILENAME = "offline-demo.log"


def booking_agent_home() -> Path:
    override = os.environ.get("BOOKING_AGENT_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".booking-agent"


def marker_path() -> Path:
    return booking_agent_home() / MARKER_FILENAME


def stack_pid_path() -> Path:
    return booking_agent_home() / STACK_PID_FILENAME


def stack_port_path() -> Path:
    return booking_agent_home() / STACK_PORT_FILENAME


def watcher_pid_path() -> Path:
    return booking_agent_home() / WATCHER_PID_FILENAME


def log_path() -> Path:
    return booking_agent_home() / LOG_FILENAME


def env_path() -> Path:
    return booking_agent_home() / ".env"
