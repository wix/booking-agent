"""Configuration lookup: process environment first, then the shipped defaults files.

Each package ships a ``defaults.env`` next to its code listing every variable it reads.
Entries after a ``# [demo]`` line apply only while demo defaults are enabled. A
commented ``# NAME=`` line documents a variable that has no default.
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

CORE_DEFAULTS = Path(__file__).with_name("defaults.env")
DEMO_MARKER = "# [demo]"
_PLACEHOLDER = re.compile(r"^#\s?([A-Z][A-Z0-9_]*)=(.*)$")
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_files: List[Path] = [CORE_DEFAULTS]
_demo = False


@dataclass(frozen=True)
class Entry:
    name: str
    value: Optional[str]
    demo: bool
    file: Path


def register_defaults(path: Path) -> None:
    """Add another package's shipped defaults file (later files win on conflicts)."""
    path = Path(path)
    if path not in _files:
        _files.append(path)
        entries.cache_clear()


def set_demo(enabled: bool) -> None:
    global _demo
    _demo = bool(enabled)


def demo_enabled() -> bool:
    return _demo


def _parse(path: Path) -> List[Entry]:
    text = path.read_text(encoding="utf-8")
    main, _, demo = text.partition(f"\n{DEMO_MARKER}\n")
    out: List[Entry] = []
    for section, is_demo in ((main, False), (demo, True)):
        for name, value in dotenv_values(stream=io.StringIO(section)).items():
            out.append(Entry(name, value, is_demo, path))
        for line in section.splitlines():
            match = _PLACEHOLDER.match(line.strip())
            if match:
                out.append(Entry(match.group(1), None, is_demo, path))
    return out


@lru_cache(maxsize=1)
def entries() -> Tuple[Entry, ...]:
    out: List[Entry] = []
    for path in _files:
        out.extend(_parse(path))
    return tuple(out)


def known_names() -> frozenset:
    return frozenset(e.name for e in entries())


def _defaults(demo: bool) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for entry in entries():
        if entry.value is not None and not entry.demo:
            values[entry.name] = entry.value
    if demo:
        for entry in entries():
            if entry.value is not None and entry.demo:
                values[entry.name] = entry.value
    return values


def shipped_default(name: str) -> Optional[str]:
    return _defaults(_demo).get(name)


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
    """Folder for sessions, tokens, and traces: ``BOOKING_AGENT_SESSION_DIR`` or ~/.booking-agent."""
    session_dir = get("BOOKING_AGENT_SESSION_DIR")
    return Path(session_dir) if session_dir else Path.home() / ".booking-agent"


def google_calendar_token_dir() -> Path:
    """Saved Google Calendar tokens, shared by every reader and writer."""
    cache_dir = get("GOOGLE_CALENDAR_TOKEN_CACHE_DIR") or ".google-calendar-tokens"
    if os.path.isabs(cache_dir):
        return Path(cache_dir)
    return booking_agent_home() / cache_dir
