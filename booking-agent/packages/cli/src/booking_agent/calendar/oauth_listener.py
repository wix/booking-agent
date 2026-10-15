"""Out-of-process Google OAuth callback listener.

``start`` binds the callback port in the calling process, hands the listening
socket to a detached child, records the child, and returns. The calling command
therefore exits right away, and a harness that kills it cannot take the listener
down with it or leave the port bound: the child stops on its own at its deadline.

``status`` and ``cleanup`` read and clear the record, so a later command can
report the result or release a listener that is no longer wanted.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from booking_agent.secure_file import ensure_private_dir, write_private_text

STATUS_PENDING = "pending"
STATUS_CONNECTED = "connected"
STATUS_TIMEOUT = "timeout"
STATUS_FAILED = "failed"
STATUS_NONE = "none"

# A child is only signalled while it can still be ours: past this grace the pid may
# belong to an unrelated process, and the child has already exited on its own.
_KILL_GRACE_SECONDS = 30
_BIND_HOST = "0.0.0.0"


class CallbackPortBusy(RuntimeError):
    """The OAuth redirect port is held by another process."""

    def __init__(self, port: int):
        super().__init__(f"Port {port} is already in use, so the sign-in callback cannot start.")
        self.port = port


def _state_dir() -> Path:
    from booking_agent.settings import booking_agent_home

    return booking_agent_home() / ".calendar-oauth"


def _record_path() -> Path:
    return _state_dir() / "listener.json"


def _result_path() -> Path:
    return _state_dir() / "result.json"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(20):
        if not pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _record_owns_live_child(record: Dict[str, Any]) -> bool:
    pid = int(record.get("pid") or 0)
    deadline = float(record.get("deadline") or 0)
    return time.time() < deadline + _KILL_GRACE_SECONDS and pid_alive(pid)


def cleanup() -> bool:
    """Stop a recorded listener and forget it. True when a running child was stopped."""
    record = _read_json(_record_path())
    stopped = False
    if record and _record_owns_live_child(record):
        _terminate(int(record["pid"]))
        stopped = True
    _remove(_record_path())
    _remove(_result_path())
    return stopped


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name != "nt":
        # Only lets a port in TIME_WAIT be reused; a live listener still refuses the bind.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((_BIND_HOST, port))
        sock.listen(16)
    except OSError as exc:
        sock.close()
        raise CallbackPortBusy(port) from exc
    return sock


def start(
    *,
    port: int,
    state: str,
    pkce: Dict[str, str],
    mode: str,
    timeout: int,
) -> Dict[str, Any]:
    """Start the callback child for one sign-in attempt and return its record."""
    cleanup()
    ensure_private_dir(_state_dir())
    sock = _bind(port)
    deadline = time.time() + timeout
    payload = {
        "state": state,
        "pkce": pkce,
        "deadline": deadline,
        "port": port,
        "result_path": str(_result_path()),
    }
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": dict(os.environ),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0,
        )
        sock.close()
    else:
        sock.set_inheritable(True)
        payload["fd"] = sock.fileno()
        kwargs["pass_fds"] = (sock.fileno(),)
        kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(
            [sys.executable, "-m", "booking_agent.calendar.oauth_listener"], **kwargs,
        )
        assert child.stdin is not None
        child.stdin.write(json.dumps(payload).encode("utf-8"))
        child.stdin.close()
    finally:
        if os.name != "nt":
            sock.close()
    record = {
        "pid": child.pid,
        "port": port,
        "mode": mode,
        "state": state,
        "deadline": deadline,
        "started_at": time.time(),
    }
    write_private_text(_record_path(), json.dumps(record))
    return record


def status() -> Dict[str, Any]:
    """Current sign-in attempt: ``pending``, ``connected``, ``timeout``, ``failed``, or ``none``."""
    record = _read_json(_record_path())
    if not record:
        return {"status": STATUS_NONE}
    out = {"status": STATUS_PENDING, "mode": record.get("mode"), "deadline": record.get("deadline")}
    result = _read_json(_result_path())
    if result and result.get("state") == record.get("state"):
        out["status"] = result.get("status") or STATUS_FAILED
        if result.get("error"):
            out["error"] = result["error"]
        return out
    if time.time() >= float(record.get("deadline") or 0):
        out["status"] = STATUS_TIMEOUT
        return out
    if not pid_alive(int(record.get("pid") or 0)):
        out["status"] = STATUS_FAILED
        out["error"] = "The sign-in listener stopped before Google Calendar was connected."
    return out


# ---------------------------------------------------------------------------
# Child process
# ---------------------------------------------------------------------------


def _write_result(path: str, state: str, status_value: str, error: Optional[str] = None) -> None:
    body: Dict[str, Any] = {"state": state, "status": status_value}
    if error:
        body["error"] = error
    write_private_text(Path(path), json.dumps(body))


async def _serve(payload: Dict[str, Any]) -> None:
    import uvicorn

    from booking_agent.calendar import google

    state = payload["state"]
    google._PKCE_STORE[state] = dict(payload["pkce"])
    if "fd" in payload:
        config = uvicorn.Config(google._build_callback_app(), fd=int(payload["fd"]), log_level="warning")
    else:
        config = uvicorn.Config(
            google._build_callback_app(), host=_BIND_HOST, port=int(payload["port"]), log_level="warning",
        )
    server = uvicorn.Server(config)
    result_path = payload["result_path"]
    deadline = float(payload["deadline"])

    async def _watch() -> None:
        while not server.should_exit:
            if state in google._TOKEN_STORE:
                _write_result(result_path, state, STATUS_CONNECTED)
                # Let the success page reach the browser before the listener stops.
                await asyncio.sleep(1.5)
                server.should_exit = True
                return
            if google._OAUTH_FAILURES.get(state):
                _write_result(result_path, state, STATUS_FAILED, google._OAUTH_FAILURES[state])
                await asyncio.sleep(1.5)
                server.should_exit = True
                return
            if time.time() >= deadline:
                _write_result(result_path, state, STATUS_TIMEOUT)
                server.should_exit = True
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(_watch())
    try:
        await server.serve()
    except (OSError, SystemExit) as exc:
        _write_result(result_path, state, STATUS_FAILED, f"The sign-in listener could not start ({type(exc).__name__}).")
    finally:
        watcher.cancel()


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    asyncio.run(_serve(payload))


if __name__ == "__main__":
    main()
