"""Load env files into the process for one command, in priority order.

Highest first: the process environment, ``~/.booking-agent/.env``, ``./.env``. Each
layer only fills names a higher layer left unset. The shipped ``defaults.env`` is the
lowest layer and is read through ``booking_agent.settings`` instead of being copied
into the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import dotenv_values

from booking_agent import settings

SOURCE_PROCESS = "process environment"
SOURCE_USER = "user .env"
SOURCE_FOLDER = "folder .env"
SOURCE_SHIPPED = "shipped default"

# Names this module put into os.environ, with the value and the layer they came from.
_applied: Dict[str, Tuple[str, str]] = {}
_loaded_files: List[Tuple[str, Path]] = []
_ignored_runtime: List[Tuple[str, Path, str]] = []


@dataclass(frozen=True)
class Layer:
    source: str
    path: Path


def user_env_path() -> Path:
    return settings.booking_agent_home() / ".env"


def layers() -> List[Layer]:
    return [
        Layer(SOURCE_USER, user_env_path()),
        Layer(SOURCE_FOLDER, Path.cwd() / ".env"),
    ]


def reset() -> None:
    """Remove every value this module put into the environment."""
    for name, (value, _source) in _applied.items():
        if os.environ.get(name) == value:
            del os.environ[name]
    _applied.clear()
    _loaded_files.clear()
    _ignored_runtime.clear()


def load() -> None:
    """Re-apply the layers for this command. Safe to call more than once per process."""
    reset()
    for layer in layers():
        if not layer.path.is_file():
            continue
        _loaded_files.append((layer.source, layer.path))
        for name, value in dotenv_values(layer.path).items():
            if name in settings.RUNTIME_ONLY:
                _ignored_runtime.append((layer.source, layer.path, name))
                continue
            if value is None or name in os.environ:
                continue
            os.environ[name] = value
            _applied[name] = (value, layer.source)


def source_of(name: str) -> str:
    if name in _applied and os.environ.get(name) == _applied[name][0]:
        return _applied[name][1]
    if name in os.environ:
        return SOURCE_PROCESS
    if settings.shipped_default(name) is None:
        return "unset"
    return SOURCE_SHIPPED


def process_value(name: str) -> Optional[str]:
    """The value the process started with; a value an env file supplied is never returned."""
    if name in _applied and os.environ.get(name) == _applied[name][0]:
        return None
    return os.environ.get(name)


def loaded_files() -> List[Tuple[str, Path]]:
    return list(_loaded_files)


def unknown_keys() -> List[Tuple[str, Path, str]]:
    """Names in the loaded env files that nothing reads (for example renamed variables)."""
    known = settings.known_names()
    out = []
    for source, path in _loaded_files:
        for name in dotenv_values(path):
            if name not in known:
                out.append((source, path, name))
    return out


def ignored_runtime_keys() -> List[Tuple[str, Path, str]]:
    """Per-process names found in an env file. They are never read from one."""
    return list(_ignored_runtime)
