"""Whether booking-agent-cli files came from this repository's release workflow (PEP 740).

``release.yml`` signs every published file with a Sigstore certificate for its GitHub
Actions identity and uploads that attestation to PyPI. This module checks it:

* ``download_verified_wheel`` fetches a version's wheel from PyPI and verifies its
  provenance before anything installs it (the self-updater installs that exact file);
* ``verify_installed`` checks every installed file of this CLI against the ``RECORD``
  of the attested wheel for its own version, so a tampered first install is caught too.

Trust comes from Sigstore's public roots and the publisher identity
(``wix/booking-agent``, workflow ``release.yml``), never from the index alone: a file
PyPI serves without a matching attestation is refused.
"""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from . import __version__ as CLI_VERSION

PACKAGE = "booking-agent-cli"
DIST_NAME = "booking_agent_cli"
EXPECTED_REPOSITORY = "wix/booking-agent"
EXPECTED_WORKFLOW = "release.yml"
REPOSITORY_URL = f"https://github.com/{EXPECTED_REPOSITORY}"
# release.yml publishes .devN versions to TestPyPI and every other version to PyPI.
PYPI = ("https://pypi.org", "files.pythonhosted.org")
TESTPYPI = ("https://test.pypi.org", "test-files.pythonhosted.org")
FETCH_TIMEOUT_SECONDS = 30.0
# Import packages the wheel installs; any other .py file inside them was not shipped.
TOP_PACKAGES = ("booking_agent", "booking_agent_cli")

INSTALL_EDITABLE = "editable"
INSTALL_LOCAL = "local"
INSTALL_INDEX = "index"


class ProvenanceError(Exception):
    """A file could not be shown to come from the release workflow.

    ``transient`` marks failures to reach PyPI, which say nothing about the files.
    """

    def __init__(self, reason: str, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.transient = transient


def index_for(version: str) -> tuple:
    """``(base URL, files host)`` of the index ``release.yml`` publishes ``version`` to."""
    return TESTPYPI if ".dev" in version else PYPI


def wheel_filename(version: str) -> str:
    return f"{DIST_NAME}-{version}-py3-none-any.whl"


def reinstall_command(version: str = CLI_VERSION) -> str:
    return f"uv tool install --force {PACKAGE}=={version}"


# ---------------------------------------------------------------------------
# Verifying a wheel
# ---------------------------------------------------------------------------


def verify_distribution(
    filename: str,
    sha256: str,
    provenance: bytes,
    *,
    repository: str = EXPECTED_REPOSITORY,
    workflow: str = EXPECTED_WORKFLOW,
    offline: bool = False,
) -> None:
    """Raise ``ProvenanceError`` unless ``provenance`` attests ``filename`` with ``sha256``.

    Every bundle must name this repository and workflow as its publisher; the bundle's
    own claim is checked against the expected identity before its certificate is.
    """
    from pydantic import ValidationError
    from pypi_attestations import (
        AttestationError,
        Distribution,
        GitHubPublisher,
        Provenance,
    )

    try:
        parsed = Provenance.model_validate_json(provenance)
        dist = Distribution(name=filename, digest=sha256)
    except (ValidationError, ValueError) as exc:
        raise ProvenanceError("provenance_invalid", f"Provenance is not valid: {exc}") from exc
    if not parsed.attestation_bundles:
        raise ProvenanceError("provenance_missing", "Provenance has no attestations.")
    for bundle in parsed.attestation_bundles:
        publisher = bundle.publisher
        if not isinstance(publisher, GitHubPublisher):
            raise ProvenanceError(
                "wrong_publisher", f"Signed by a {publisher.kind} publisher, not GitHub."
            )
        if publisher.repository != repository or publisher.workflow != workflow:
            raise ProvenanceError(
                "wrong_publisher",
                f"Signed by {publisher.repository} ({publisher.workflow}), "
                f"not {repository} ({workflow}).",
            )
        if not bundle.attestations:
            raise ProvenanceError("provenance_missing", "A provenance bundle has no attestations.")
        for attestation in bundle.attestations:
            try:
                attestation.verify(publisher, dist, offline=offline)
            except AttestationError as exc:
                raise ProvenanceError("signature_invalid", str(exc)) from exc
            except Exception as exc:  # sigstore raises its own types for bad bundles
                raise ProvenanceError("signature_invalid", f"Verification failed: {exc}") from exc


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# PyPI
# ---------------------------------------------------------------------------


def _http_get(url: str, *, accept: Optional[str] = None) -> Any:
    """GET ``url``; the response object has ``status_code`` and ``content``."""
    import httpx

    from booking_agent.http_deadlines import coerce_timeout

    headers = {"Accept": accept} if accept else {}
    return httpx.get(url, headers=headers, timeout=coerce_timeout(FETCH_TIMEOUT_SECONDS), follow_redirects=False)


def _get(url: str, *, accept: Optional[str] = None, missing: str, what: str) -> bytes:
    try:
        resp = _http_get(url, accept=accept)
    except Exception as exc:
        raise ProvenanceError("network", f"Could not reach PyPI for {what}: {type(exc).__name__}", transient=True) from exc
    if resp.status_code == 404:
        raise ProvenanceError(missing, f"PyPI has no {what}.")
    if resp.status_code != 200:
        raise ProvenanceError(
            "network", f"PyPI answered HTTP {resp.status_code} for {what}.", transient=resp.status_code >= 500
        )
    return resp.content


def wheel_listing(version: str) -> Dict[str, str]:
    """``{"url", "sha256", "filename"}`` of a version's wheel in the PyPI simple index."""
    filename = wheel_filename(version)
    base, files_host = index_for(version)
    raw = _get(
        f"{base}/simple/{PACKAGE}/",
        accept="application/vnd.pypi.simple.v1+json",
        missing="not_on_index",
        what=f"{PACKAGE} index",
    )
    try:
        files = json.loads(raw).get("files") or []
    except (ValueError, AttributeError) as exc:
        raise ProvenanceError("network", "PyPI returned an unreadable index.", transient=True) from exc
    for entry in files:
        if isinstance(entry, dict) and entry.get("filename") == filename:
            url = entry.get("url") or ""
            sha256 = (entry.get("hashes") or {}).get("sha256") or ""
            parts = urlsplit(url)
            if parts.scheme != "https" or parts.hostname != files_host:
                raise ProvenanceError("untrusted_url", f"PyPI listed {filename} at an unexpected location.")
            if not sha256:
                raise ProvenanceError("not_on_index", f"PyPI lists no hash for {filename}.")
            return {"url": url, "sha256": sha256, "filename": filename}
    raise ProvenanceError("not_on_index", f"{filename} is not on PyPI.")


def fetch_provenance(version: str, filename: str) -> bytes:
    base, _ = index_for(version)
    return _get(
        f"{base}/integrity/{PACKAGE}/{version}/{filename}/provenance",
        missing="provenance_missing",
        what=f"provenance for {filename}",
    )


def download_verified_wheel(version: str, dest_dir: Path) -> Path:
    """Download ``version``'s wheel into ``dest_dir`` and return it once its provenance verifies."""
    listing = wheel_listing(version)
    data = _get(listing["url"], missing="not_on_index", what=listing["filename"])
    sha256 = hashlib.sha256(data).hexdigest()
    if sha256 != listing["sha256"]:
        raise ProvenanceError("digest_mismatch", f"{listing['filename']} does not match the hash PyPI lists.")
    verify_distribution(listing["filename"], sha256, fetch_provenance(version, listing["filename"]))
    path = Path(dest_dir) / listing["filename"]
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# The installed distribution
# ---------------------------------------------------------------------------


def _distribution() -> Any:
    from importlib.metadata import distribution

    return distribution(PACKAGE)


def install_source() -> str:
    """``editable`` (source checkout), ``local`` (a wheel or folder on disk), or ``index``."""
    try:
        raw = _distribution().read_text("direct_url.json")
    except Exception:
        return INSTALL_INDEX
    if not raw:
        return INSTALL_INDEX
    try:
        info = json.loads(raw)
    except ValueError:
        return INSTALL_INDEX
    if (info.get("dir_info") or {}).get("editable"):
        return INSTALL_EDITABLE
    url = info.get("url") or ""
    # A wheel file (such as the one the self-updater verified and installed) is still
    # checked: its version is on PyPI with provenance.
    if url.startswith("file:") and not url.endswith(".whl"):
        return INSTALL_LOCAL
    return INSTALL_INDEX


def installed_fingerprint() -> Optional[str]:
    """SHA-256 of the installed ``RECORD``, which changes with every (re)install."""
    try:
        raw = _distribution().read_text("RECORD")
    except Exception:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else None


def _installed_root() -> Path:
    """The folder ``RECORD`` paths are relative to (site-packages)."""
    return Path(_distribution().locate_file(""))


def _record_hashes(wheel: Path) -> Dict[str, str]:
    """``RECORD`` of ``wheel``: path -> urlsafe-base64 SHA-256 (files listed without a hash skipped)."""
    with zipfile.ZipFile(wheel) as zf:
        names = [n for n in zf.namelist() if n.endswith(".dist-info/RECORD")]
        if len(names) != 1:
            raise ProvenanceError("provenance_invalid", "The attested wheel has no single RECORD.")
        text = zf.read(names[0]).decode("utf-8")
    hashes = {}
    for line in text.splitlines():
        path, _, rest = line.partition(",")
        digest = rest.split(",", 1)[0]
        if path and digest.startswith("sha256="):
            hashes[path] = digest[len("sha256="):]
    return hashes


def _record_digest(path: Path) -> str:
    raw = bytes.fromhex(file_sha256(path))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass
class IntegrityResult:
    ok: bool
    reason: Optional[str] = None
    message: Optional[str] = None
    transient: bool = False
    skipped: Optional[str] = None  # editable | local: not an index install, nothing to compare

    @property
    def tampered(self) -> bool:
        return not self.ok and not self.transient and self.skipped is None


def compare_installed(record: Dict[str, str], root: Path) -> Optional[ProvenanceError]:
    """The first difference between the attested ``record`` and the files under ``root``."""
    for rel, expected in sorted(record.items()):
        path = root / rel
        if not path.is_file():
            return ProvenanceError("installed_file_missing", f"{rel} is missing from the installation.")
        if _record_digest(path) != expected:
            return ProvenanceError("installed_file_modified", f"{rel} differs from the released file.")
    for package in TOP_PACKAGES:
        base = root / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel not in record:
                return ProvenanceError("unexpected_file", f"{rel} was not part of the release.")
    return None


def verify_installed(*, version: str = CLI_VERSION) -> IntegrityResult:
    """Check every installed file of this CLI against the attested wheel for ``version``."""
    source = install_source()
    if source != INSTALL_INDEX:
        return IntegrityResult(ok=True, skipped=source)
    try:
        with tempfile.TemporaryDirectory(prefix="booking-agent-verify-") as tmp:
            record = _record_hashes(download_verified_wheel(version, Path(tmp)))
        problem = compare_installed(record, _installed_root())
    except ProvenanceError as exc:
        return IntegrityResult(ok=False, reason=exc.reason, message=exc.message, transient=exc.transient)
    if problem is not None:
        return IntegrityResult(ok=False, reason=problem.reason, message=problem.message)
    return IntegrityResult(ok=True)
