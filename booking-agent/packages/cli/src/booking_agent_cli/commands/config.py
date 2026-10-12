"""`booking-agent config`: inspect effective settings and create the user override file."""
from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, Dict, List

import typer

from booking_agent import settings
from booking_agent.secure_file import FILE_MODE, ensure_private_dir

from .. import config_loader

app = typer.Typer(help="Show effective configuration, or create ~/.booking-agent/.env.")

MASKED = "<set, hidden>"
# Written once, uncommented-ready, in the calendar block of the header.
_CALENDAR_APP_NAMES = frozenset({"GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET"})
# The user .env lives in this folder, so the file cannot move it.
_PROCESS_ONLY_NAMES = frozenset({"BOOKING_AGENT_SESSION_DIR"})
_SECRET_NAME = re.compile(r"SECRET|TOKEN|BEARER|PASSWORD|_KEY\b|KEYFILE|SPT", re.IGNORECASE)

USER_ENV_HEADER = """\
# booking-agent overrides for this install. Created by `booking-agent config init`.
#
# Nothing here is required for a basic booking: every setting already has a shipped
# default (see `booking-agent config show`). Uncomment a line only to change it.
# Never put calendar, Link, or booking tokens in this file.

# --- Google Calendar (operator setup) ---------------------------------------------
# Calendar checking with a local sign-in needs your OAuth app's credentials:
# GOOGLE_CALENDAR_CLIENT_ID=
# GOOGLE_CALENDAR_CLIENT_SECRET=
"""


def _is_secret(name: str) -> bool:
    return bool(_SECRET_NAME.search(name))


def _calendar_status() -> str:
    if (os.environ.get("GOOGLECALENDAR_ACCESS_TOKEN") or "").strip():
        return "connected, token from host environment"
    token_dir = settings.google_calendar_token_dir()
    if token_dir.is_dir() and any(token_dir.glob("*.json")):
        return "connected, from saved token"
    if (settings.get("GOOGLE_CALENDAR_CLIENT_ID") or "").strip():
        return "not connected, local sign-in available"
    return "not connected, no calendar app configured"


def _variables() -> List[Dict[str, Any]]:
    out = []
    for name in sorted(settings.known_names()):
        value = settings.get(name)
        if value is not None and value != "" and _is_secret(name):
            value = MASKED
        out.append({"name": name, "value": value, "source": config_loader.source_of(name)})
    return out


@app.command("show")
def config_show() -> None:
    """Print every setting's effective value and where it came from (secrets hidden)."""
    config_loader.load()
    link_bin = settings.get("LINK_CLI_BIN") or "link-cli"
    report = {
        "files": [{"source": s, "path": str(p)} for s, p in config_loader.loaded_files()],
        "variables": _variables(),
        "unknown_keys": [
            {"source": s, "path": str(p), "name": n} for s, p, n in config_loader.unknown_keys()
        ],
        "ignored_runtime_keys": [
            {"source": s, "path": str(p), "name": n}
            for s, p, n in config_loader.ignored_runtime_keys()
        ],
        "runtime": {
            "calendar": _calendar_status(),
            "link_cli": "found on PATH" if shutil.which(link_bin) else "not found on PATH",
        },
    }
    typer.echo(json.dumps(report, indent=2))


@app.command("init")
def config_init() -> None:
    """Create ~/.booking-agent/.env with commented-out overrides. Never overwrites."""
    path = config_loader.user_env_path()
    if path.exists():
        typer.echo(json.dumps({"written": False, "path": str(path), "reason": "already exists"}))
        return
    lines = [USER_ENV_HEADER, "\n# --- Shipped defaults (uncomment to override) ---------------------------------\n"]
    for entry in settings.entries():
        if entry.value is None or entry.name in _CALENDAR_APP_NAMES | _PROCESS_ONLY_NAMES:
            continue
        lines.append(f"# {entry.name}={entry.value}\n")
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))
    typer.echo(json.dumps({"written": True, "path": str(path)}))
