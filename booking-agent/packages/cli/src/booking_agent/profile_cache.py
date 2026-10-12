"""File-based cache for resolved business profiles across CLI invocations."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

from . import settings
from .secure_file import ensure_private_dir, write_private_text

if TYPE_CHECKING:
    from .discovery import USPBusinessProfile
    from .ucp_profile import UcpNativeContext

logger = logging.getLogger(__name__)

_CACHE_FILE_NAME = "profile_cache.json"

#: Bumped when the cached shape gains a field whose *absence* changes behaviour.
#: v2 added ``USPBusinessProfile.authorization``: a profile cached under v1 has no
#: policy recorded, which is indistinguishable from a business that publishes
#: none -- and that routes privileged calls onto the fail-closed path.  Entries
#: written by an older version are therefore discarded rather than trusted.
_CACHE_FORMAT_VERSION = 2

_store_lock = threading.Lock()


def _ttl_seconds() -> int:
    return settings.get_int("BOOKING_AGENT_PROFILE_CACHE_TTL_SECONDS", minimum=0)


def cache_path() -> Path:
    base = settings.booking_agent_home()
    ensure_private_dir(base)
    return base / _CACHE_FILE_NAME


def _cache_key(profile_url: str) -> str:
    return hashlib.sha256(profile_url.strip().encode("utf-8")).hexdigest()


def _load_store() -> Dict[str, Any]:
    path = cache_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read profile cache at %s", path, exc_info=True)
        return {}


def _save_store(store: Dict[str, Any]) -> None:
    path = cache_path()
    try:
        write_private_text(path, json.dumps(store, indent=2))
    except OSError:
        logger.warning("Failed to write profile cache to %s", path, exc_info=True)


def read_cached_profile(
    profile_url: str,
) -> Optional[Tuple["USPBusinessProfile", Optional["UcpNativeContext"]]]:
    """Return cached profile if present and not expired."""
    from .discovery import USPBusinessProfile
    from .ucp_profile import UcpNativeContext

    ttl = _ttl_seconds()
    if ttl <= 0:
        return None

    with _store_lock:
        store = _load_store()
        entry = store.get(_cache_key(profile_url))
        if not isinstance(entry, dict):
            return None
        if entry.get("profile_url") != profile_url:
            return None
        if entry.get("format_version") != _CACHE_FORMAT_VERSION:
            logger.info(
                "Discarding profile cache entry for %s written in an older format",
                profile_url,
            )
            return None

        cached_at = entry.get("cached_at", 0)
        if time.time() - float(cached_at) > ttl:
            return None

        usp_raw = entry.get("usp_profile")
        if not isinstance(usp_raw, dict):
            return None

        profile = USPBusinessProfile.model_validate(usp_raw)
        uctx: Optional[UcpNativeContext] = None
        uctx_raw = entry.get("ucp_context")
        if isinstance(uctx_raw, dict):
            uctx = UcpNativeContext.model_validate(uctx_raw)

    logger.info("Profile cache hit for %s", profile_url)
    return profile, uctx


def write_cached_profile(
    profile_url: str,
    profile: "USPBusinessProfile",
    uctx: Optional["UcpNativeContext"],
) -> None:
    """Persist a resolved profile for cross-command reuse."""
    ttl = _ttl_seconds()
    if ttl <= 0:
        return

    with _store_lock:
        store = _load_store()
        store[_cache_key(profile_url)] = {
            "profile_url": profile_url,
            "format_version": _CACHE_FORMAT_VERSION,
            "cached_at": time.time(),
            "usp_profile": profile.model_dump(by_alias=True),
            "ucp_context": uctx.model_dump() if uctx is not None else None,
        }
        _save_store(store)
    logger.info("Profile cached for %s", profile_url)
