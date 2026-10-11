"""Idempotent teardown for the offline UCP demo stack."""
from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .offline_demo_paths import (
    env_path,
    log_path,
    marker_path,
    stack_pid_path,
    stack_port_path,
    watcher_pid_path,
)


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}\n"
    try:
        log_path().parent.mkdir(parents=True, exist_ok=True)
        with log_path().open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    print(msg, flush=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill_pid(pid: int, *, grace_sec: float = 2.0) -> None:
    if pid <= 0 or not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + grace_sec
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.1)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def read_marker() -> Optional[Dict[str, Any]]:
    path = marker_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def restore_env_from_backup(backup_path: str) -> None:
    dest = env_path()
    src = Path(backup_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        _log(f"Restored {dest} from {src}")
    elif dest.exists():
        dest.unlink()
        _log(f"Removed {dest} (no backup file at {src})")


def remove_marker_files() -> None:
    for path in (
        marker_path(),
        stack_pid_path(),
        stack_port_path(),
        watcher_pid_path(),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def teardown_offline_demo(
    *,
    reason: str = "manual",
    marker: Optional[Dict[str, Any]] = None,
) -> bool:
    """Stop stack + watcher, restore ``.env``, remove marker files. Returns True if work ran."""
    meta = marker if marker is not None else read_marker()
    if meta is None:
        stack_pid = _read_int_pid(stack_pid_path())
        watcher_pid = _read_int_pid(watcher_pid_path())
        if not stack_pid and not watcher_pid:
            return False
        meta = {"stack_pid": stack_pid, "watcher_pid": watcher_pid, "env_backup": ""}

    stack_pid = int(meta.get("stack_pid") or 0)
    watcher_pid = int(meta.get("watcher_pid") or 0)
    env_backup = str(meta.get("env_backup") or "")

    if stack_pid:
        _kill_pid(stack_pid)
    if watcher_pid and watcher_pid != os.getpid():
        _kill_pid(watcher_pid)

    if env_backup:
        restore_env_from_backup(env_backup)

    remove_marker_files()
    _log(f"Offline demo torn down ({reason}). Live defaults restored.")
    return True


def _read_int_pid(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else 0
    except (OSError, ValueError):
        return 0
