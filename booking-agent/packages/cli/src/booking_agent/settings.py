"""Configuration lookup: process environment first, then the shipped defaults file.

``defaults.env`` next to this module lists every configuration variable the package
reads. A commented ``# NAME=`` line documents a variable that has no default.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import dotenv_values

DEFAULTS = Path(__file__).with_name("defaults.env")
_PLACEHOLDER = re.compile(r"^#\s?([A-Z][A-Z0-9_]*)=(.*)$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Values a host injects into one process. They are never configuration, so they are
#: not in defaults.env and the env-file loader ignores them.
RUNTIME_ONLY = frozenset({"GOOGLECALENDAR_ACCESS_TOKEN", "BOOKING_AGENT_BUYER_DEVICE_IP"})


@dataclass(frozen=True)
class Entry:
    name: str
    value: Optional[str]


@lru_cache(maxsize=1)
def entries() -> Tuple[Entry, ...]:
    text = DEFAULTS.read_text(encoding="utf-8")
    out = [Entry(name, value) for name, value in dotenv_values(stream=io.StringIO(text)).items()]
    for line in text.splitlines():
        match = _PLACEHOLDER.match(line.strip())
        if match:
            out.append(Entry(match.group(1), None))
    return tuple(out)


def known_names() -> frozenset:
    return frozenset(e.name for e in entries()) | RUNTIME_ONLY


@lru_cache(maxsize=1)
def _defaults() -> Dict[str, str]:
    return {e.name: e.value for e in entries() if e.value is not None}


def shipped_default(name: str) -> Optional[str]:
    return _defaults().get(name)


def get(name: str, default: Optional[str] = None) -> Optional[str]:
    """The process value when set (even empty), else the shipped default, else ``default``."""
    if name in os.environ:
        return os.environ[name]
    value = shipped_default(name)
    return default if value is None else value


def _number(name: str, kind, minimum):
    for raw in (get(name), shipped_default(name)):
        if raw is None or not str(raw).strip():
            continue
        try:
            value = kind(raw)
        except ValueError:
            continue
        return value if minimum is None else max(minimum, value)
    raise KeyError(f"{name} has no usable value and no shipped default")


def get_int(name: str, *, minimum: Optional[int] = None) -> int:
    """An integer setting; an unparsable value falls back to the shipped default."""
    return _number(name, int, minimum)


def get_float(name: str, *, minimum: Optional[float] = None) -> float:
    return _number(name, float, minimum)


def get_bool(name: str) -> bool:
    return (get(name) or "").strip().lower() in _TRUTHY


def booking_agent_home() -> Path:
    """Folder for sessions, tokens, and traces (``BOOKING_AGENT_SESSION_DIR``).

    ``~`` expands to the user's home on every OS (``USERPROFILE`` on Windows).
    """
    raw = (get("BOOKING_AGENT_SESSION_DIR") or "").strip() or shipped_default("BOOKING_AGENT_SESSION_DIR")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(
            f"BOOKING_AGENT_SESSION_DIR resolves to {str(path)!r}, which is not an absolute "
            "path. Set it to an absolute folder, or give this user a home directory."
        )
    return path


def google_calendar_token_dir() -> Path:
    """Saved Google Calendar tokens, shared by every reader and writer."""
    cache_dir = get("GOOGLE_CALENDAR_TOKEN_CACHE_DIR") or ".google-calendar-tokens"
    if os.path.isabs(cache_dir):
        return Path(cache_dir)
    return booking_agent_home() / cache_dir
