"""Whether to tell the user about an update, and what to install.

Two inputs decide it:

* the local pair: the skill version the agent loaded (``--skill-version``) against the
  skill this CLI was built with. A mismatch needs no network and is always asked about.
* the release index (``release-notes/index.json``): each release's change type, rollout
  status, and one-sentence summary. It may recommend or hold back an update; it never
  stops an installed CLI from working.

The installed CLI makes the decision. The agent relays ``user_summary`` and never reads
release notes to decide on its own.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from booking_agent import settings

from . import __version__ as CLI_VERSION
from .skill_bundle import SKILL_VERSION

# Must equal the values scripts/release_notes.py accepts (tested).
CHANGE_TYPES = ("fix", "feature", "breaking")
UPDATE_STATUSES = ("available", "recommended", "urgent", "deferred", "withdrawn")
CAPABILITIES = ("search", "sign_in", "calendar", "booking", "payment", "cancel", "reschedule")
# A verified harness configures its replay id. The harness registry stays outside the
# portable CLI; two harnesses promote together by configuring the same replay id.
REPLAY_ENV = "BOOKING_AGENT_REPLAY"

ACTION_NONE = "none"
ACTION_INFORM = "inform"
ACTION_RECOMMEND = "recommend"
ACTION_URGENT = "urgent"
PROMPT_ACTIONS = (ACTION_RECOMMEND, ACTION_URGENT)

AWAITING_UPDATE_CONSENT = "update_consent"
UPDATE_COMMAND = "booking-agent update --user-confirmed"
CHECK_COMMAND = "booking-agent update check"

CACHE_FILE = "release-index.json"
CACHE_TTL_SECONDS = 24 * 3600
FETCH_TIMEOUT_SECONDS = 3.0

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?$")
_REPLAY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_URGENCY = {"available": 0, "recommended": 1, "urgent": 2}
_SEVERITY = {"fix": 0, "feature": 1, "breaking": 2}


def parse_version(text: Any) -> Optional[Tuple[int, int, int, int]]:
    """``MAJOR.MINOR.PATCH[.devN]`` as a sortable tuple; a dev build sorts before its release."""
    match = _VERSION.match(text.strip()) if isinstance(text, str) else None
    if not match:
        return None
    major, minor, patch, dev = match.groups()
    return int(major), int(minor), int(patch), int(dev) if dev is not None else 1 << 30


@dataclass(frozen=True)
class Release:
    cli_version: str
    skill_version: str
    change_type: str
    update_status: str
    ecosystem_ready: bool
    relevant_capabilities: Tuple[str, ...]
    user_summary: str
    details_url: str
    replacement_version: Optional[str] = None
    replay_rollouts: Dict[str, Tuple[str, bool]] = field(default_factory=dict)

    @property
    def key(self) -> Tuple[int, int, int, int]:
        return parse_version(self.cli_version)  # type: ignore[return-value]


def parse_index(doc: Any) -> List[Release]:
    """Releases from an index document. Malformed entries are skipped, never trusted."""
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        return []
    out: Dict[str, Release] = {}
    for raw in doc.get("releases") or []:
        release = _parse_release(raw)
        if release is not None:
            out[release.cli_version] = release
    return sorted(out.values(), key=lambda r: r.key)


def _parse_release(raw: Any) -> Optional[Release]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return None
    cli, skill = raw.get("cli_version"), raw.get("skill_version")
    if parse_version(cli) is None or parse_version(skill) is None:
        return None
    change, status = raw.get("change_type"), raw.get("update_status")
    if change not in CHANGE_TYPES or status not in UPDATE_STATUSES:
        return None
    ready = raw.get("ecosystem_ready")
    summary, url = raw.get("user_summary"), raw.get("details_url")
    if not isinstance(ready, bool) or not isinstance(summary, str) or not isinstance(url, str):
        return None
    if status not in ("deferred", "withdrawn") and not ready:
        status = "deferred"
    caps = raw.get("relevant_capabilities")
    caps = tuple(c for c in caps if c in CAPABILITIES) if isinstance(caps, list) else ()
    replacement = raw.get("replacement_version")
    if status == "withdrawn" and parse_version(replacement) is None:
        replacement = None
    return Release(
        cli_version=cli,
        skill_version=skill,
        change_type=change,
        update_status=status,
        ecosystem_ready=ready,
        relevant_capabilities=caps,
        user_summary=summary.strip()[:200],
        details_url=url if url.startswith("https://github.com/wix/booking-agent/") else "",
        replacement_version=replacement if status == "withdrawn" else None,
        replay_rollouts=_parse_replay_rollouts(raw.get("replay_rollouts")),
    )


def configured_replay() -> Optional[str]:
    raw = settings.get(REPLAY_ENV)
    replay_id = raw.strip() if isinstance(raw, str) else ""
    return replay_id if _REPLAY_ID.fullmatch(replay_id) else None


def _parse_replay_rollouts(raw: Any) -> Dict[str, Tuple[str, bool]]:
    """Well-formed replay rollouts; malformed records are safely ignored."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Tuple[str, bool]] = {}
    for replay_id, item in raw.items():
        if (
            not isinstance(replay_id, str)
            or not _REPLAY_ID.fullmatch(replay_id)
            or not isinstance(item, dict)
        ):
            continue
        status, ready = item.get("update_status"), item.get("ecosystem_ready")
        if status not in UPDATE_STATUSES or not isinstance(ready, bool):
            continue
        if status not in ("deferred", "withdrawn") and not ready:
            status = "deferred"
        out[replay_id] = (status, ready)
    return out


def effective_release(release: Release, replay_id: Optional[str]) -> Release:
    """The rollout this runtime is allowed to act on.

    A configured replay never inherits the general status. A missing record stays deferred.
    A generally withdrawn release stays withdrawn for every harness.
    """
    if replay_id is None or release.update_status == "withdrawn":
        return release
    rollout = release.replay_rollouts.get(replay_id)
    if rollout is None:
        status, ready = "deferred", False
    else:
        status, ready = rollout
    return replace(release, update_status=status, ecosystem_ready=ready)


def releases_for_replay(releases: Sequence[Release], replay_id: Optional[str]) -> List[Release]:
    return [effective_release(release, replay_id) for release in releases]


@dataclass
class Decision:
    action: str = ACTION_NONE
    reason: str = "up_to_date"
    target_version: Optional[str] = None
    target_skill_version: Optional[str] = None
    change_type: Optional[str] = None
    update_status: Optional[str] = None
    user_summary: Optional[str] = None
    details_url: Optional[str] = None
    relevant_capabilities: List[str] = field(default_factory=list)
    prompt_key: Optional[str] = None
    replay: Optional[str] = None

    def as_data(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "installed_version": CLI_VERSION,
            "installed_skill_version": SKILL_VERSION,
            "target_version": self.target_version,
            "target_skill_version": self.target_skill_version,
            "change_type": self.change_type,
            "update_status": self.update_status,
            "user_summary": self.user_summary,
            "details_url": self.details_url,
            "relevant_capabilities": list(self.relevant_capabilities),
            "prompt_key": self.prompt_key,
            "replay": self.replay,
        }


def local_pair_reason(reported: Optional[str]) -> Optional[str]:
    """Why the loaded skill does not match this CLI, or None when it does."""
    if reported is None or not reported.strip():
        return "skill_unknown"
    loaded, built = parse_version(reported), parse_version(SKILL_VERSION)
    if loaded is None or built is None:
        return "skill_unknown"
    if loaded < built:
        return "skill_older"
    if loaded > built:
        return "skill_newer"
    return None


def select_target(installed: str, releases: Sequence[Release]) -> Tuple[Optional[Release], List[Release]]:
    """The newest release that is safe to move to, and the ready releases that move brings in.

    A deferred release is skipped, not a stopping point: it holds back only itself, so
    a later recommended or urgent release is still offered. ``releases`` carries either
    the general rollout or one replay's effective status; both are read the same way.
    CLIs before 0.11.2 stop at the first deferred release, which is why the release
    notes recommend every release up to 0.11.2 for each verified replay.
    """
    current = parse_version(installed)
    if current is None:
        return None, []
    target: Optional[Release] = None
    brought: List[Release] = []
    for release in releases:
        if release.key <= current or release.update_status == "deferred":
            continue
        brought.append(release)
        if release.update_status != "withdrawn":
            target = release
    if target is None:
        return None, []
    return target, [r for r in brought if r.key <= target.key]


def decide(
    *,
    reported_skill: Optional[str],
    releases: Optional[Sequence[Release]],
    answered: Iterable[str] = (),
    capabilities: Iterable[str] = (),
    after_booking: bool = False,
    check_local: bool = True,
    store_too_new: bool = False,
    installed: str = CLI_VERSION,
    replay_id: Optional[str] = None,
) -> Decision:
    answered = set(answered)
    replay_id = replay_id.strip() if isinstance(replay_id, str) else ""
    replay_id = replay_id if _REPLAY_ID.fullmatch(replay_id) else None
    releases = releases_for_replay(list(releases or []), replay_id)
    target, brought = select_target(installed, releases)
    decision = _remote_decision(installed, releases, target, brought, set(capabilities), after_booking)
    decision.replay = replay_id
    if replay_id and decision.prompt_key:
        decision.prompt_key = f"{decision.prompt_key}:{replay_id}"

    if store_too_new:
        decision.action, decision.reason = ACTION_URGENT, "saved_state_from_newer_version"
        decision.prompt_key = None
        return decision

    if check_local:
        reason = local_pair_reason(reported_skill)
        if reason is not None:
            key = f"skill-pair:{(reported_skill or 'none').strip()}:{SKILL_VERSION}"
            if key not in answered:
                decision.action, decision.reason, decision.prompt_key = ACTION_URGENT, reason, key
                if decision.user_summary is None:
                    decision.user_summary = "Brings the booking instructions in line with the installed assistant."
                return decision

    if decision.prompt_key and decision.prompt_key in answered:
        decision.action, decision.reason = ACTION_NONE, "already_answered"
    return decision


def _remote_decision(
    installed: str,
    releases: List[Release],
    target: Optional[Release],
    brought: List[Release],
    capabilities: set,
    after_booking: bool,
) -> Decision:
    current = next((r for r in releases if r.cli_version == installed), None)
    if current is not None and current.update_status == "withdrawn":
        replacement = next((r for r in releases if r.cli_version == current.replacement_version), None)
        if replacement is not None and target is not None and target.key >= replacement.key:
            return _decision_for(target, brought, ACTION_URGENT, "installed_version_withdrawn")
    if target is None:
        return Decision()

    urgency = max(_URGENCY.get(r.update_status, 0) for r in brought)
    severity = max(_SEVERITY[r.change_type] for r in brought)
    relevant = bool(capabilities & {c for r in brought for c in r.relevant_capabilities})
    if urgency == _URGENCY["urgent"]:
        action, reason = ACTION_URGENT, "urgent_release"
    elif urgency == _URGENCY["recommended"] and severity == _SEVERITY["breaking"]:
        action, reason = ACTION_RECOMMEND, "ready_breaking_release"
    elif urgency == _URGENCY["recommended"] and (after_booking or relevant):
        action, reason = ACTION_RECOMMEND, "relevant_release" if relevant else "recommended_release"
    elif urgency == _URGENCY["recommended"]:
        action, reason = ACTION_INFORM, "recommended_release"
    else:
        action, reason = ACTION_NONE, "optional_release"
    return _decision_for(target, brought, action, reason)


def _decision_for(target: Release, brought: List[Release], action: str, reason: str) -> Decision:
    lead = max(brought, key=lambda r: (_URGENCY.get(r.update_status, 0), r.key))
    return Decision(
        action=action,
        reason=reason,
        target_version=target.cli_version,
        target_skill_version=target.skill_version,
        change_type=max((r.change_type for r in brought), key=_SEVERITY.__getitem__),
        update_status=lead.update_status,
        user_summary=lead.user_summary,
        details_url=target.details_url or None,
        relevant_capabilities=sorted({c for r in brought for c in r.relevant_capabilities}),
        prompt_key=f"release:{target.cli_version}",
    )


# ---------------------------------------------------------------------------
# Fetching the index
# ---------------------------------------------------------------------------

def checks_enabled() -> bool:
    return settings.get_bool("BOOKING_AGENT_UPDATE_CHECK")


def _cache_path() -> Path:
    return settings.booking_agent_home() / CACHE_FILE


def _http_get(url: str) -> Any:
    import httpx

    from booking_agent.http_deadlines import coerce_timeout

    resp = httpx.get(url, timeout=coerce_timeout(FETCH_TIMEOUT_SECONDS), follow_redirects=False)
    resp.raise_for_status()
    return resp.json()


def load_releases(*, refresh: bool = False, now: Optional[float] = None) -> Optional[List[Release]]:
    """The release index, from a cache younger than a day or the network; None if unavailable."""
    if not checks_enabled():
        return None
    now = time.time() if now is None else now
    cache = _cache_path()
    cached = _read_cache(cache)
    if cached is not None and not refresh and now - cached[0] < CACHE_TTL_SECONDS:
        return parse_index(cached[1])
    url = settings.get("BOOKING_AGENT_RELEASE_INDEX_URL") or ""
    if not url.startswith("https://"):
        return parse_index(cached[1]) if cached else None
    try:
        doc = _http_get(url)
    except Exception:
        return parse_index(cached[1]) if cached else None
    try:
        from booking_agent.secure_file import write_private_text

        write_private_text(cache, json.dumps({"fetched_at": now, "index": doc}))
    except Exception:
        pass
    return parse_index(doc)


def _read_cache(path: Path) -> Optional[Tuple[float, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return float(raw["fetched_at"]), raw["index"]
    except Exception:
        return None
