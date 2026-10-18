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
import shutil
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


def _move_children(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        shutil.move(str(child), str(dst / child.name))


def link_one(runtime: Path, persistent: Path, *, private: bool) -> Dict[str, str]:
    """Make ``runtime`` a symlink to ``persistent``. Returns what happened."""
    out = {"runtime": str(runtime), "persistent": str(persistent)}
    if private:
        ensure_private_dir(persistent)
    else:
        persistent.mkdir(parents=True, exist_ok=True)

    relinked = False
    if runtime.is_symlink():
        if Path(os.readlink(runtime)) == persistent or runtime.resolve() == persistent.resolve():
            out["result"] = "already_linked"
            return out
        # Linked into another root (a different conversation chose a different folder).
        # Bring that root's saved state along when this one has none; otherwise leave it alone.
        previous = runtime.resolve()
        if previous.is_dir() and not _is_empty_dir(previous):
            if _is_empty_dir(persistent):
                _move_children(previous, persistent)
                out["moved_existing_contents"] = "true"
            else:
                out["previous_root_left_at"] = str(previous)
        runtime.unlink()
        relinked = True
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
    out["result"] = "relinked" if relinked else "linked"
    return out


def normalized_root(persistent_root: str) -> Path:
    """The folder both state and the sign-in live under.

    Agents often pass the state folder itself (``.../booking-agent``) as the root. Because
    ``prepare`` adds its own subfolders, that used to create ``.../booking-agent/booking-agent``
    and a second sign-in folder, splitting the saved state. A root already named after the
    state folder means its parent.
    """
    root = _absolute(persistent_root, what="--persistent-root")
    if root.name == STATE_SUBDIR:
        root = root.parent
    return root


def _lift_nested_layout(root: Path) -> Dict[str, str]:
    """Undo the old nested layout (``<root>/booking-agent/{booking-agent,link-cli}``)."""
    state = root / STATE_SUBDIR
    lifted: Dict[str, str] = {}
    nested_state = state / STATE_SUBDIR
    if nested_state.is_dir():
        for child in list(nested_state.iterdir()):
            if not (state / child.name).exists():
                os.replace(child, state / child.name)
                lifted["lifted_from_nested_layout"] = "true"
        if _is_empty_dir(nested_state):
            nested_state.rmdir()
    nested_link = state / LINK_SUBDIR
    if nested_link.is_dir():
        target = root / LINK_SUBDIR
        if not target.exists() or _is_empty_dir(target):
            if target.exists():
                target.rmdir()
            os.replace(nested_link, target)
            lifted["lifted_from_nested_layout"] = "true"
    return lifted


def prepare(persistent_root: str, raw_links: List[str]) -> List[Dict[str, str]]:
    root = normalized_root(persistent_root)
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise PrepareError(f"--persistent-root {root} is not a folder.")
    ensure_private_dir(root / STATE_SUBDIR)
    lifted = _lift_nested_layout(root)
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
    if lifted and results:
        results[0].update(lifted)
    return results


def _describe(path: Path) -> Dict[str, object]:
    linked = path.is_symlink()
    return {
        "path": str(path),
        "linked_to": str(path.resolve()) if linked else None,
        "exists": path.exists(),
    }


def diagnose() -> Dict[str, object]:
    """Read-only report of where state lives and anything left behind by an earlier layout."""
    state = settings.booking_agent_home()
    link = link_cli_config_dir()
    state_info, link_info = _describe(state), _describe(link)
    findings: List[Dict[str, str]] = []
    problems = False
    root_hint = ""

    if state_info["linked_to"]:
        target = Path(str(state_info["linked_to"]))
        root_hint = str(target.parent)
        for sub, kind in ((STATE_SUBDIR, "nested_state_folder"), (LINK_SUBDIR, "nested_link_folder")):
            if (target / sub).is_dir():
                problems = True
                findings.append({"kind": kind, "path": str(target / sub), "severity": "problem"})
    else:
        findings.append({"kind": "not_persistent", "path": str(state), "severity": "info"})
    if state_info["linked_to"] and link_info["linked_to"]:
        if Path(str(state_info["linked_to"])).parent != Path(str(link_info["linked_to"])).parent:
            problems = True
            findings.append({"kind": "state_and_sign_in_in_different_roots", "path": str(link), "severity": "problem"})
    for runtime in (state, link):
        for aside in sorted(runtime.parent.glob(f"{runtime.name}.before-prepare-*")) if runtime.parent.is_dir() else []:
            findings.append({"kind": "runtime_copy_kept_aside", "path": str(aside), "severity": "info"})

    return {
        "state": state_info,
        "link_sign_in": link_info,
        "findings": findings,
        "healthy": not problems,
        "fix_command": (
            f"booking-agent runtime prepare --persistent-root {root_hint}" if problems and root_hint else None
        ),
    }
