"""Whether this installation may book and take payments: the first-run provenance check.

The first ``flow init`` after an install or update checks the installed files against
the attested release (``provenance.verify_installed``) and remembers the outcome for
that exact installation (its ``RECORD`` fingerprint). A mismatch is remembered too, so
``flow book`` and every ``flow pay`` command refuse until the CLI is reinstalled. A
failure to reach PyPI decides nothing and is retried on the next check.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import __version__ as CLI_VERSION
from . import provenance

CODE = "installation_unverified"


def _same_install(record: Any, fingerprint: Optional[str]) -> bool:
    return (
        isinstance(record, dict)
        and record.get("version") == CLI_VERSION
        and record.get("record_sha256") == fingerprint
    )


def check(store: Any) -> Optional[Dict[str, Any]]:
    """The recorded failure for this installation, or None when it may proceed."""
    from .session import save_store

    fingerprint = provenance.installed_fingerprint()
    if _same_install(store.install_integrity_failure, fingerprint):
        return store.install_integrity_failure
    if _same_install(store.install_verified, fingerprint):
        return None
    result = provenance.verify_installed()
    if result.skipped is not None or result.transient:
        return None
    if result.ok:
        store.install_verified = {"version": CLI_VERSION, "record_sha256": fingerprint}
        store.install_integrity_failure = None
    else:
        store.install_integrity_failure = {
            "version": CLI_VERSION,
            "record_sha256": fingerprint,
            "reason": result.reason,
            "detail": result.message,
        }
    save_store(store)
    return store.install_integrity_failure


def error_message() -> str:
    return (
        "The booking assistant installed on this device does not match the official "
        "release, so it will not book or take payments. Tell the user in plain words that "
        "the assistant needs to be reinstalled before it can book, and reinstall it only "
        "with their yes, by running data.reinstall_command. Do not try to book another way."
    )


def error_data(failure: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "reason": failure.get("reason"),
        "detail": failure.get("detail"),
        "installed_version": CLI_VERSION,
        "reinstall_command": provenance.reinstall_command(),
    }
