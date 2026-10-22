"""Point this runtime's state folders at a folder that outlives it.

Some agent runtimes start each conversation from a fresh filesystem but keep one
folder between conversations. ``prepare`` links the Booking Agent state folder
and the Link credential folder (plus any extra pairs the agent names) into that
folder, so sessions, saved preferences, and the Link sign-in survive a restart.

Only symlinks are created. Existing contents are moved, never copied or
deleted, so file modes on credentials and keys stay as they were.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from booking_agent import settings
from booking_agent.secure_file import ensure_private_dir, write_private_text

STATE_SUBDIR = "booking-agent"
LINK_SUBDIR = "link-cli"
_LINK_CLI_APP = "link-cli-nodejs"
ROOT_ENV = "BOOKING_AGENT_PERSISTENT_ROOT"
# A host that keeps this runtime's files itself declares so with ``BOOKING_AGENT_PERSISTENCE=none``.
PERSISTENCE_ENV = "BOOKING_AGENT_PERSISTENCE"
_ACK_FILE = ".persistence-none"
_ATTEMPTS_FILE = ".persistence-attempts"
_BOOT_FILE = ".booking-agent-boot"
# How many times the CLI sends the agent back after a folder fails the checks. The next one is
# accepted, so a stubborn agent can never leave a booking stuck on this question.
MAX_CORRECTIONS = 2
_TEMP_ROOTS = ("/tmp", "/var/tmp", "/dev/shm", "/run")
_MEMORY_FILESYSTEMS = frozenset({"tmpfs", "ramfs", "devtmpfs"})
_SESSION_FILE = ".usp-session.json"
# Order a booking moves through, used to keep the further-along copy of the same session.
_PHASE_ORDER = (
    "INIT", "PREREQ_OK", "BUSINESS_SELECTED", "CALENDAR_GATE", "AVAILABILITY_QUERIED",
    "SLOTS_PRESENTED", "SLOT_SELECTED", "BUYER_RESOLVED", "BOOKED", "PAYMENT_PENDING",
    "CONFIRMED", "FAILED",
)


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


def _phase_rank(session: Dict[str, object]) -> int:
    phase = session.get("flow_phase")
    return _PHASE_ORDER.index(phase) if phase in _PHASE_ORDER else -1


def merge_session_stores(newer: Dict[str, object], older: Dict[str, object]) -> Dict[str, object]:
    """Union of two saved booking files, by session id, without dropping any booking.

    A session present in both keeps the copy that got further (a stale ``INIT`` snapshot never
    hides the finished booking). Everything else at the top level comes from ``newer`` and falls
    back to ``older``. Works on the raw JSON so fields this version does not know survive.
    """
    merged: Dict[str, object] = {**older, **{k: v for k, v in newer.items() if v is not None}}
    by_id: Dict[object, Dict[str, object]] = {}
    order: List[object] = []
    for source in (older, newer):
        for session in source.get("sessions") or []:
            if not isinstance(session, dict):
                continue
            sid = session.get("id")
            if sid not in by_id:
                by_id[sid] = session
                order.append(sid)
            elif _phase_rank(session) >= _phase_rank(by_id[sid]):
                by_id[sid] = session
    merged["sessions"] = [by_id[sid] for sid in order]
    for key in ("sessions_created", "schema_version"):
        values = [v for v in (newer.get(key), older.get(key)) if isinstance(v, int)]
        if values:
            merged[key] = max(values)
    answered = []
    for source in (older, newer):
        for item in source.get("update_prompts_answered") or []:
            if isinstance(item, str) and item not in answered:
                answered.append(item)
    if answered:
        merged["update_prompts_answered"] = answered
    active = newer.get("active_session_id") or older.get("active_session_id")
    merged["active_session_id"] = active if active in by_id else (order[-1] if order else None)
    return merged


def _merge_session_files(src: Path, dst: Path) -> None:
    def _load(path: Path) -> Dict[str, object]:
        try:
            raw = json.loads(path.read_text())
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    src_newer = src.stat().st_mtime >= dst.stat().st_mtime
    a, b = _load(src), _load(dst)
    merged = merge_session_stores(a, b) if src_newer else merge_session_stores(b, a)
    write_private_text(dst, json.dumps(merged, indent=2, sort_keys=False))
    src.unlink()


def _merge_tree(src: Path, dst: Path, aside: Path, kept: List[str], rel: str = "") -> None:
    """Move everything from ``src`` into ``dst``; nothing is deleted.

    A name only in ``src`` moves over. Two folders merge. Two booking files merge by session.
    Any other pair keeps the newer file at ``dst`` and moves the older one under ``aside``.
    """
    for child in sorted(src.iterdir()):
        target = dst / child.name
        here = f"{rel}/{child.name}" if rel else child.name
        if not (target.exists() or target.is_symlink()):
            shutil.move(str(child), str(target))
        elif child.is_dir() and target.is_dir():
            _merge_tree(child, target, aside, kept, here)
            if _is_empty_dir(child):
                child.rmdir()
        elif child.name == _SESSION_FILE and child.is_file() and target.is_file():
            _merge_session_files(child, target)
        elif (
            child.is_file() and target.is_file()
            and child.stat().st_mtime > target.stat().st_mtime
        ):
            # The incoming file is newer: the older one at the destination is kept aside.
            shutil.move(str(target), str(_aside_path(aside, here, kept)))
            shutil.move(str(child), str(target))
        else:
            shutil.move(str(child), str(_aside_path(aside, here, kept)))


def _aside_path(aside: Path, rel: str, kept: List[str]) -> Path:
    path = aside / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    kept.append(rel)
    return path


def merge_dir_into(src: Path, dst: Path, *, aside_name: str) -> Dict[str, str]:
    """Merge folder ``src`` into ``dst``, then remove the (now empty) ``src``."""
    aside = src.with_name(aside_name)
    aside.mkdir(parents=True, exist_ok=True)
    kept: List[str] = []
    _merge_tree(src, dst, aside, kept)
    if _is_empty_dir(src):
        src.rmdir()
    out = {"merged_existing_contents": "true"}
    if kept:
        out["older_copies_kept_at"] = str(aside)
    else:
        try:
            aside.rmdir()
        except OSError:
            pass
    return out


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
        if previous.is_dir() and not _is_empty_dir(previous) and previous != persistent.resolve():
            if _is_empty_dir(persistent):
                _move_children(previous, persistent)
                out["moved_existing_contents"] = "true"
            else:
                out.update(merge_dir_into(
                    previous, persistent, aside_name=f"{previous.name}.before-prepare-{int(time.time())}",
                ))
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
            # Both hold data: merge them. Booking files merge by session id; for any other
            # file the newer copy stays and the older one is kept aside, never deleted.
            out.update(merge_dir_into(
                runtime, persistent, aside_name=f"{runtime.name}.before-prepare-{int(time.time())}",
            ))

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
    """Undo the old nested layout (``<root>/booking-agent/{booking-agent,link-cli}``).

    Both folders are merged into their proper place, so a stale top-level booking file never
    hides the real one from the nested folder.
    """
    state = root / STATE_SUBDIR
    lifted: Dict[str, str] = {}
    stamp = int(time.time())
    nested_state = state / STATE_SUBDIR
    if nested_state.is_dir():
        merge_dir_into(nested_state, state, aside_name=f"{STATE_SUBDIR}.before-prepare-{stamp}")
        lifted["lifted_from_nested_layout"] = "true"
    nested_link = state / LINK_SUBDIR
    if nested_link.is_dir():
        target = root / LINK_SUBDIR
        if target.exists() and not _is_empty_dir(target):
            merge_dir_into(nested_link, target, aside_name=f"{LINK_SUBDIR}.before-prepare-{stamp}")
        else:
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
    touch_boot_marker(root)
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
        unlinked = find_unlinked_state()
        if unlinked is not None:
            problems = True
            root_hint = str(unlinked)
            findings.append({"kind": "saved_state_not_linked", "path": root_hint, "severity": "problem"})
    if state_info["linked_to"] and link_info["linked_to"]:
        if Path(str(state_info["linked_to"])).parent != Path(str(link_info["linked_to"])).parent:
            problems = True
            findings.append({"kind": "state_and_sign_in_in_different_roots", "path": str(link), "severity": "problem"})
    for runtime in (state, link):
        for aside in sorted(runtime.parent.glob(f"{runtime.name}.before-prepare-*")) if runtime.parent.is_dir() else []:
            findings.append({"kind": "runtime_copy_kept_aside", "path": str(aside), "severity": "info"})

    configured = configured_root()
    if configured is not None:
        try:
            expected = normalized_root(configured) / STATE_SUBDIR
        except PrepareError:
            expected = None
        applied = bool(
            expected is not None and state_info["linked_to"]
            and Path(str(state_info["linked_to"])) == expected.resolve()
        )
        if not applied:
            problems = True
            findings.append({
                "kind": "configured_root_not_applied", "path": str(state), "severity": "problem",
            })
            root_hint = root_hint or configured
    persistence: Dict[str, object] = {
        "folder": root_hint or None, "survived_restart": None, "survived_restart_basis": None,
    }
    starts = runtime_starts(state)
    persistence["runtime_starts"] = starts
    if root_hint:
        persistence["survived_restart"] = survived_restart(Path(root_hint))
        if persistence["survived_restart"] is True:
            persistence["survived_restart_basis"] = "boot_id_changed"
        elif starts >= 2:
            persistence["survived_restart"] = True
            persistence["survived_restart_basis"] = "second_runtime_read_the_saved_booking"
        elif persistence["survived_restart"] is None:
            persistence["survived_restart_basis"] = (
                "boot_id_unavailable" if current_boot_id() is None else "no_boot_marker_yet"
            )
        else:
            persistence["survived_restart_basis"] = "same_boot_id_so_not_shown"
        if persistence["survived_restart"] is False:
            findings.append({
                "kind": "folder_not_yet_shown_to_survive_a_restart",
                "path": root_hint, "severity": "info",
            })
    despite = _read_attempts().get("accepted_despite")
    if despite:
        findings.append({
            "kind": "folder_accepted_despite_checks", "path": str(despite), "severity": "info",
        })
    return {
        "persistence": persistence,
        "configured_root": configured,
        "state": state_info,
        "link_sign_in": link_info,
        "findings": findings,
        "healthy": not problems,
        "fix_command": (
            f"booking-agent runtime prepare --persistent-root {root_hint}" if problems and root_hint else None
        ),
    }


def configured_root() -> Optional[str]:
    """The persistent folder a host names with ``BOOKING_AGENT_PERSISTENT_ROOT``, if any."""
    value = (settings.get(ROOT_ENV) or "").strip()
    return value or None


def auto_prepare() -> Optional[List[Dict[str, str]]]:
    """Link state into the host-named persistent folder, quietly and idempotently.

    Nothing happens unless the host set ``BOOKING_AGENT_PERSISTENT_ROOT``. A failure never
    blocks a booking; ``runtime doctor`` reports it.
    """
    root = configured_root()
    if root is None:
        return None
    try:
        return prepare(root, [])
    except (PrepareError, OSError):
        return None


def persistence_resolution() -> Optional[str]:
    """Why ``flow init`` may go on without asking how this runtime keeps state, or None.

    ``linked``: saved state already lives in a folder the CLI linked it into.
    ``host_declared``: the host set ``BOOKING_AGENT_PERSISTENCE=none``.
    ``acknowledged``: an agent already said this runtime keeps its files (kept beside the state,
    so a fresh runtime that lost the state is asked again).
    """
    home = settings.booking_agent_home()
    if home.is_symlink():
        return "linked"
    if (settings.get(PERSISTENCE_ENV) or "").strip().lower() == "none":
        return "host_declared"
    if (home / _ACK_FILE).is_file():
        return "acknowledged"
    return None


_TERMINAL_PHASES = frozenset({"CONFIRMED", "FAILED"})
_SCAN_DEPTH_NAMES = 2


def _safe(check: Callable[[], bool]) -> bool:
    """A filesystem test that treats an unreadable path as absent (``Path.is_file`` raises on EACCES)."""
    try:
        return bool(check())
    except OSError:
        return False


def _holds_unfinished_booking(session_file: Path) -> bool:
    try:
        raw = json.loads(session_file.read_text())
    except (OSError, ValueError):
        return False
    sessions = raw.get("sessions") if isinstance(raw, dict) else None
    return any(
        isinstance(s, dict) and s.get("flow_phase") not in _TERMINAL_PHASES
        for s in sessions or []
    )


def find_unlinked_state() -> Optional[Path]:
    """A persistent root that holds an unfinished booking this runtime has not linked.

    A restart can wipe the runtime's own folders (and the symlinks into the persistent one),
    leaving the saved booking and Link sign-in intact but invisible: the CLI then sees "no
    session" and an agent starts over. Looks beside the working folder, its parents, and the
    home folder, at most two levels down, for a folder this CLI prepared earlier (it holds
    ``booking-agent/.usp-session.json`` and a boot marker or the sign-in folder).
    """
    home = settings.booking_agent_home()
    if _safe(home.is_symlink) or _safe((home / _SESSION_FILE).is_file):
        return None
    try:
        bases = [Path.cwd(), *Path.cwd().parents, Path.home()]
    except (OSError, RuntimeError):
        return None
    seen = set()
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        level = [base]
        for _ in range(_SCAN_DEPTH_NAMES + 1):
            nxt = []
            for folder in level:
                state = folder / STATE_SUBDIR
                session_file = state / _SESSION_FILE
                if (
                    _safe(session_file.is_file)
                    and (_safe((folder / _BOOT_FILE).is_file) or _safe((folder / LINK_SUBDIR).is_dir))
                    and _holds_unfinished_booking(session_file)
                ):
                    return folder
                try:
                    nxt.extend(
                        child for child in sorted(folder.iterdir())
                        if _safe(child.is_dir) and not _safe(child.is_symlink)
                        and child.name not in ("node_modules", ".git", ".venv", "proc", "sys")
                    )
                except OSError:
                    continue
            level = nxt if len(nxt) <= 200 else []
    return None


def saved_state_not_linked_message(root: Path) -> str:
    return (
        f"A saved booking and Link sign-in exist in {root}, but this runtime is not linked to "
        "them (a restart cleared the links). Do not start over and do not use "
        "--no-persistent-root: run next_command once, then continue with `booking-agent flow "
        "status`."
    )


def acknowledge_no_persistence() -> None:
    home = ensure_private_dir(settings.booking_agent_home())
    write_private_text(home / _ACK_FILE, time.strftime("%Y-%m-%d\n", time.gmtime()))


# ---------------------------------------------------------------------------
# Checks on the folder an agent names, and the record of how often it was sent back
# ---------------------------------------------------------------------------

def ephemeral_roots() -> List[Path]:
    """Folders whose contents are not kept between runs, resolved."""
    roots = [Path(p) for p in _TEMP_ROOTS] + [Path(tempfile.gettempdir())]
    resolved = []
    for root in roots:
        try:
            resolved.append(root.resolve())
        except OSError:
            continue
    return resolved


def fs_type(path: Path) -> Optional[str]:
    """Filesystem type holding ``path`` (Linux only; None when it cannot be told)."""
    try:
        target = path.resolve()
        best, best_type = "", None
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                fields = left.split()
                if len(fields) < 5 or not right:
                    continue
                mount = fields[4].replace("\\040", " ")
                if (str(target) == mount or str(target).startswith(mount.rstrip("/") + "/")) and len(mount) > len(best):
                    best, best_type = mount, right.split()[0]
        return best_type
    except OSError:
        return None


def durability_problem(raw: str) -> Optional[str]:
    """Why the folder an agent named cannot be a place that survives restarts, or None.

    Only what can be known now: a path that cannot work, or one the OS itself says is not kept.
    A folder on ordinary disk that the host simply never restores cannot be told apart here.
    """
    try:
        path = _absolute(raw, what="The folder")
    except PrepareError as exc:
        return str(exc)
    try:
        resolved = path.resolve()
    except OSError:
        return "the path cannot be resolved"
    if resolved == Path(resolved.anchor):
        return "it is the filesystem root"
    for root in ephemeral_roots():
        if resolved == root or root in resolved.parents:
            return f"it is under {root}, which is cleared between runs"
    kind = fs_type(resolved)
    if kind in _MEMORY_FILESYSTEMS:
        return f"it is on {kind}, which lives in memory"
    ancestor = resolved
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        return f"{ancestor} is a file, not a folder"
    if not os.access(ancestor, os.W_OK | os.X_OK):
        return f"{ancestor} is not writable"
    return None


def _attempts_path() -> Path:
    return settings.booking_agent_home() / _ATTEMPTS_FILE


def _read_attempts() -> Dict[str, object]:
    try:
        raw = json.loads(_attempts_path().read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def corrections_so_far() -> int:
    value = _read_attempts().get("rejected")
    return value if isinstance(value, int) else 0


def record_correction() -> int:
    """Count one more time the agent was sent back; returns the new count."""
    n = corrections_so_far() + 1
    ensure_private_dir(settings.booking_agent_home())
    write_private_text(_attempts_path(), json.dumps({"rejected": n}))
    return n


def record_accepted_despite(problem: str) -> None:
    ensure_private_dir(settings.booking_agent_home())
    # The count starts over: this folder was used as given, and a later bad folder is a new
    # decision that gets its own corrections instead of being waved through.
    write_private_text(
        _attempts_path(),
        json.dumps({"rejected": 0, "accepted_despite": problem}),
    )


def clear_attempts() -> None:
    try:
        _attempts_path().unlink()
    except OSError:
        pass


def current_boot_id() -> Optional[str]:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def touch_boot_marker(root: Path) -> None:
    """Remember which boot first used this folder, so a later boot proves it survived a restart."""
    boot = current_boot_id()
    if boot is None:
        return
    marker = root / _BOOT_FILE
    try:
        previous = json.loads(marker.read_text())
    except (OSError, ValueError):
        previous = {}
    first = previous.get("first") or boot
    data = {"first": first, "last": boot, "survived": bool(previous.get("survived")) or first != boot}
    try:
        write_private_text(marker, json.dumps(data))
    except OSError:
        pass


def survived_restart(root: Path) -> Optional[bool]:
    """True once this folder has been seen across two boots; None when boots cannot be told.

    Never a false "yes": the marker lives in the folder, so seeing it again means the folder
    came back. It can stay False after a real restart when sandboxes share one kernel (many
    containers report the host's boot id), so False means "not shown", not "lost".
    """
    try:
        previous = json.loads((root / _BOOT_FILE).read_text())
    except (OSError, ValueError):
        return None
    boot = current_boot_id()
    if boot is None:
        return None
    return bool(previous.get("survived")) or previous.get("first") != boot


# ---------------------------------------------------------------------------
# "Has this runtime looked at the saved booking yet?"
# ---------------------------------------------------------------------------

RESUME_CHECK_ENV = "BOOKING_AGENT_RESUME_CHECK"


def _resume_marker(session_file: Path) -> Path:
    import hashlib

    key = hashlib.sha256(str(session_file).encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"booking-agent-seen-{key}"


def resume_check_enabled() -> bool:
    return (settings.get(RESUME_CHECK_ENV) or "").strip().lower() not in ("0", "false", "off", "no")


def runtime_has_seen(session_file: Path) -> bool:
    """False in a runtime that has not read this saved booking since it started.

    The marker lives in the temporary folder, which a fresh sandbox does not keep while the
    folder holding the booking is kept. A conversation that starts from a saved booking must
    read ``flow status`` before it changes anything, because the saved question is the only
    thing a bare "yes" can answer.
    """
    return _resume_marker(session_file).exists()


_STARTS_FILE = ".runtime-starts"


def runtime_starts(home: Optional[Path] = None) -> int:
    """How many runtimes have read the saved booking, counted in the kept folder."""
    try:
        return int(json.loads(((home or settings.booking_agent_home()) / _STARTS_FILE).read_text())["count"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def mark_runtime_seen(session_file: Path) -> None:
    """Remember that this runtime read the saved booking; count each new runtime.

    The count lives beside the saved booking, so a second runtime reading the same folder
    proves it survived a restart even on hosts whose sandboxes share one boot id.
    """
    marker = _resume_marker(session_file)
    if marker.exists():
        return
    try:
        home = session_file.parent
        write_private_text(home / _STARTS_FILE, json.dumps({"count": runtime_starts(home) + 1}))
    except OSError:
        pass
    try:
        marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except OSError:
        pass
