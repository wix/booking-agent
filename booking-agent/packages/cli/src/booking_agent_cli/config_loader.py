"""Load env files into the process for one command, in priority order.

Highest first: the process environment, the session's env file (``--env-file`` at
init, or ``BOOKING_AGENT_ENV_FILE``), ``~/.booking-agent/.env``, ``./.env``. Each layer
only fills names a higher layer left unset. The shipped ``defaults.env`` files are the
lowest layer and are read through ``booking_agent.settings`` instead of being copied
into the environment.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import dotenv_values

from booking_agent import settings

ENV_FILE_VAR = "BOOKING_AGENT_ENV_FILE"
SOURCE_PROCESS = "process environment"
SOURCE_ENV_FILE = "env file"
SOURCE_USER = "user .env"
SOURCE_FOLDER = "folder .env"
SOURCE_SHIPPED = "shipped default"

# Names this module put into os.environ, with the value and the layer they came from.
_applied: Dict[str, Tuple[str, str]] = {}
_loaded_files: List[Tuple[str, Path]] = []


class EnvFileNotFound(FileNotFoundError):
    pass


@dataclass(frozen=True)
class Layer:
    source: str
    path: Path


def user_env_path() -> Path:
    return settings.booking_agent_home() / ".env"


def resolve_env_file(env_file: Optional[str]) -> Optional[Path]:
    """Absolute path of an explicit env file, or None. Raises when it does not exist."""
    if not env_file:
        return None
    path = Path(env_file).expanduser().resolve()
    if not path.is_file():
        raise EnvFileNotFound(f"env file not found: {env_file}")
    return path


def chosen_env_file(env_file: Optional[str]) -> Optional[Path]:
    """The explicit env file, else ``BOOKING_AGENT_ENV_FILE`` from the real environment."""
    return resolve_env_file(env_file or _unapplied_process_value(ENV_FILE_VAR))


def layers(*, env_file: Optional[str]) -> List[Layer]:
    out: List[Layer] = []
    chosen = chosen_env_file(env_file)
    if chosen is not None:
        out.append(Layer(SOURCE_ENV_FILE, chosen))
    out.append(Layer(SOURCE_USER, user_env_path()))
    out.append(Layer(SOURCE_FOLDER, Path.cwd() / ".env"))
    return out


def _unapplied_process_value(name: str) -> Optional[str]:
    if name in _applied and os.environ.get(name) == _applied[name][0]:
        return None
    return os.environ.get(name)


def reset() -> None:
    """Remove every value this module put into the environment."""
    for name, (value, _source) in _applied.items():
        if os.environ.get(name) == value:
            del os.environ[name]
    _applied.clear()
    _loaded_files.clear()


def load(*, env_file: Optional[str] = None) -> None:
    """Re-apply the layers for this command. Safe to call more than once per process."""
    reset()
    for layer in layers(env_file=env_file):
        if not layer.path.is_file():
            continue
        _loaded_files.append((layer.source, layer.path))
        for name, value in dotenv_values(layer.path).items():
            if value is None or name in os.environ:
                continue
            os.environ[name] = value
            _applied[name] = (value, layer.source)


@contextlib.contextmanager
def preserved():
    """Run a block that loads another session's config, then restore this one's."""
    saved_env = dict(os.environ)
    saved_applied = dict(_applied)
    saved_files = list(_loaded_files)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        _applied.clear()
        _applied.update(saved_applied)
        _loaded_files[:] = saved_files


def load_for_session(session) -> None:
    if session is None:
        load()
        return
    load(env_file=session.env_file)


def source_of(name: str) -> str:
    if name in _applied and os.environ.get(name) == _applied[name][0]:
        return _applied[name][1]
    if name in os.environ:
        return SOURCE_PROCESS
    if settings.shipped_default(name) is None:
        return "unset"
    return SOURCE_SHIPPED


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
