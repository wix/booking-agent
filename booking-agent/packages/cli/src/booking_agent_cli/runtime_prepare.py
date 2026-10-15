"""Point this runtime's state folders at a folder that outlives it.

Some agent runtimes start each conversation from a fresh filesystem but keep one
folder between conversations. ``prepare`` links the Booking Agent state folder
and the Link credential folder (plus any extra pairs the agent names) into that
folder, so sessions, saved preferences, and the Link sign-in survive a restart.

Only symlinks are created. Existing contents are moved, never copied or
deleted, so file modes on credentials and keys stay as they were.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from booking_agent import settings
from booking_agent.secure_file import ensure_private_dir

STATE_SUBDIR = "booking-agent"
LINK_SUBDIR = "link-cli"
_LINK_CLI_APP = "link-cli-nodejs"


class PrepareError(ValueError):
    pass


def link_cli_config_dir() -> Path:
    """Where Link CLI keeps its sign-in (the OS config folder of its ``link-cli-nodejs`` app)."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Preferences" / _LINK_CLI_APP
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
        return Path(base) / _LINK_CLI_APP / "Config"
    base = os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")
    return Path(base) / _LINK_CLI_APP


def _absolute(raw: str, *, what: str, relative_to: Optional[Path] = None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        if relative_to is None:
            raise PrepareError(f"{what} {raw!r} must be an absolute path.")
        path = relative_to / path
    return path


def parse_links(raw_links: List[str], root: Path) -> List[Tuple[Path, Path]]:
    pairs = []
    for raw in raw_links:
        runtime, sep, persistent = raw.partition("=")
        if not sep or not runtime.strip() or not persistent.strip():
            raise PrepareError(f"--link {raw!r} must look like RUNTIME_PATH=PERSISTENT_PATH.")
        pairs.append((
            _absolute(runtime.strip(), what="Runtime path"),
            _absolute(persistent.strip(), what="Persistent path", relative_to=root),
        ))
    return pairs


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def link_one(runtime: Path, persistent: Path, *, private: bool) -> Dict[str, str]:
    """Make ``runtime`` a symlink to ``persistent``. Returns what happened."""
    out = {"runtime": str(runtime), "persistent": str(persistent)}
    if private:
        ensure_private_dir(persistent)
    else:
        persistent.mkdir(parents=True, exist_ok=True)

    if runtime.is_symlink():
        if Path(os.readlink(runtime)) == persistent or runtime.resolve() == persistent.resolve():
            out["result"] = "already_linked"
            return out
        runtime.unlink()
    elif runtime.exists():
        if not runtime.is_dir():
            raise PrepareError(f"{runtime} is a file; only folders can be linked.")
        if _is_empty_dir(runtime):
            runtime.rmdir()
        elif _is_empty_dir(persistent):
            for child in runtime.iterdir():
                os.replace(child, persistent / child.name)
            runtime.rmdir()
            out["moved_existing_contents"] = "true"
        else:
            # Both hold data: the persistent copy wins, the runtime copy is kept aside.
            aside = runtime.with_name(f"{runtime.name}.before-prepare-{int(time.time())}")
            os.replace(runtime, aside)
            out["kept_runtime_copy_at"] = str(aside)

    runtime.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(persistent, runtime, target_is_directory=True)
    out["result"] = "linked"
    return out


def prepare(persistent_root: str, raw_links: List[str]) -> List[Dict[str, str]]:
    root = _absolute(persistent_root, what="--persistent-root")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise PrepareError(f"--persistent-root {root} is not a folder.")
    plan: List[Tuple[Path, Path, bool]] = [
        (settings.booking_agent_home(), root / STATE_SUBDIR, True),
        (link_cli_config_dir(), root / LINK_SUBDIR, True),
    ]
    plan += [(rt, ps, False) for rt, ps in parse_links(raw_links, root)]

    results = []
    for runtime, persistent, private in plan:
        if not runtime.is_symlink() and _is_inside(runtime, root):
            results.append({"runtime": str(runtime), "persistent": str(runtime), "result": "already_persistent"})
            continue
        results.append(link_one(runtime, persistent, private=private))
    return results
