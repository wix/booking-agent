"""PEP 740 provenance: verifying a wheel and the installed files against it."""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from booking_agent_cli import provenance

_FIXTURES = Path(__file__).resolve().parents[1] / "support" / "fixtures" / "provenance"
# Real provenance PyPI serves for this wheel; Sigstore verifies it offline (bundled trust root).
_REAL_FILE = "pypi_attestations-0.0.30-py3-none-any.whl"
_REAL_SHA256 = "b3a9c53f6cb89e5e7b5b70e6cfca97cfc66008c1ed54087355e06e40071cef21"
_REAL_REPOSITORY = "pypi/pypi-attestations"


def _real_provenance() -> bytes:
    return (_FIXTURES / f"{_REAL_FILE}.provenance.json").read_bytes()


def _verify(**overrides):
    kwargs = dict(
        filename=_REAL_FILE,
        sha256=_REAL_SHA256,
        provenance=_real_provenance(),
        repository=_REAL_REPOSITORY,
        offline=True,
    )
    kwargs.update(overrides)
    provenance.verify_distribution(**kwargs)


# ---------------------------------------------------------------------------
# verify_distribution (real Sigstore verification)
# ---------------------------------------------------------------------------


def test_a_genuine_attestation_from_the_expected_workflow_verifies():
    _verify()


def test_an_attestation_from_another_repository_is_refused():
    with pytest.raises(provenance.ProvenanceError) as exc:
        _verify(repository=provenance.EXPECTED_REPOSITORY)
    assert exc.value.reason == "wrong_publisher"


def test_an_attestation_from_another_workflow_of_the_same_repository_is_refused():
    with pytest.raises(provenance.ProvenanceError) as exc:
        _verify(workflow="build.yml")
    assert exc.value.reason == "wrong_publisher"


def test_an_attestation_for_different_bytes_is_refused():
    with pytest.raises(provenance.ProvenanceError) as exc:
        _verify(sha256="0" * 64)
    assert exc.value.reason == "signature_invalid"


def test_an_attestation_for_a_different_filename_is_refused():
    with pytest.raises(provenance.ProvenanceError) as exc:
        _verify(filename="pypi_attestations-0.0.31-py3-none-any.whl")
    assert exc.value.reason == "signature_invalid"


def test_a_forged_publisher_claim_does_not_pass_the_certificate_check():
    doc = json.loads(_real_provenance())
    for bundle in doc["attestation_bundles"]:
        bundle["publisher"]["repository"] = provenance.EXPECTED_REPOSITORY
    with pytest.raises(provenance.ProvenanceError) as exc:
        _verify(provenance=json.dumps(doc).encode(), repository=provenance.EXPECTED_REPOSITORY)
    assert exc.value.reason == "signature_invalid"


@pytest.mark.parametrize("raw, reason", [
    (b"not json", "provenance_invalid"),
    (json.dumps({"version": 1, "attestation_bundles": []}).encode(), "provenance_missing"),
])
def test_unusable_provenance_is_refused(raw, reason):
    with pytest.raises(provenance.ProvenanceError) as exc:
        _verify(provenance=raw)
    assert exc.value.reason == reason


# ---------------------------------------------------------------------------
# PyPI lookups
# ---------------------------------------------------------------------------


def _response(status=200, content=b""):
    return SimpleNamespace(status_code=status, content=content)


def _index(files):
    return _response(content=json.dumps({"files": files}).encode())


def _listed(version, *, url=None, sha256="ab" * 32):
    name = provenance.wheel_filename(version)
    return {
        "filename": name,
        "url": url or f"https://files.pythonhosted.org/packages/xx/{name}",
        "hashes": {"sha256": sha256} if sha256 else {},
    }


@pytest.fixture
def pypi(monkeypatch):
    """URL -> response (or exception) served by the fake index; anything else is a 404."""
    routes: dict = {}

    def fake_get(url, *, accept=None):
        answer = routes.get(url, _response(404))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(provenance, "_http_get", fake_get)
    return routes


def test_dev_versions_are_looked_up_on_testpypi_and_others_on_pypi():
    assert provenance.index_for("0.3.0.dev1") == provenance.TESTPYPI
    assert provenance.index_for("0.3.0") == provenance.PYPI


def test_the_listing_returns_the_wheel_url_and_hash(pypi):
    pypi["https://pypi.org/simple/booking-agent-cli/"] = _index([_listed("9.0.0")])
    listing = provenance.wheel_listing("9.0.0")
    assert listing["sha256"] == "ab" * 32
    assert listing["filename"] == "booking_agent_cli-9.0.0-py3-none-any.whl"


@pytest.mark.parametrize("entry, reason", [
    (_listed("9.0.0", url="http://files.pythonhosted.org/x.whl"), "untrusted_url"),
    (_listed("9.0.0", url="https://evil.example/x.whl"), "untrusted_url"),
    (_listed("9.0.0", sha256=None), "not_on_index"),
    (_listed("8.0.0"), "not_on_index"),
])
def test_a_listing_that_cannot_be_trusted_is_refused(pypi, entry, reason):
    pypi["https://pypi.org/simple/booking-agent-cli/"] = _index([entry])
    with pytest.raises(provenance.ProvenanceError) as exc:
        provenance.wheel_listing("9.0.0")
    assert exc.value.reason == reason


def test_an_unreachable_index_is_transient(pypi):
    pypi["https://pypi.org/simple/booking-agent-cli/"] = OSError("offline")
    with pytest.raises(provenance.ProvenanceError) as exc:
        provenance.wheel_listing("9.0.0")
    assert exc.value.reason == "network"
    assert exc.value.transient


def test_a_server_error_is_transient_but_a_client_error_is_not(pypi):
    url = "https://pypi.org/simple/booking-agent-cli/"
    pypi[url] = _response(503)
    with pytest.raises(provenance.ProvenanceError) as exc:
        provenance.wheel_listing("9.0.0")
    assert exc.value.transient
    pypi[url] = _response(403)
    with pytest.raises(provenance.ProvenanceError) as exc:
        provenance.wheel_listing("9.0.0")
    assert not exc.value.transient


def _serve_wheel(pypi, version, data, *, listed_sha256=None, provenance_body=b"{}"):
    name = provenance.wheel_filename(version)
    url = f"https://files.pythonhosted.org/packages/xx/{name}"
    sha256 = listed_sha256 or hashlib.sha256(data).hexdigest()
    pypi["https://pypi.org/simple/booking-agent-cli/"] = _index([_listed(version, url=url, sha256=sha256)])
    pypi[url] = _response(content=data)
    if provenance_body is not None:
        pypi[f"https://pypi.org/integrity/booking-agent-cli/{version}/{name}/provenance"] = _response(
            content=provenance_body
        )
    return name


def test_a_verified_wheel_is_written_with_the_downloaded_bytes(pypi, monkeypatch, tmp_path):
    name = _serve_wheel(pypi, "9.0.0", b"wheel bytes")
    seen = {}
    monkeypatch.setattr(provenance, "verify_distribution", lambda f, s, p: seen.update(f=f, s=s, p=p))
    path = provenance.download_verified_wheel("9.0.0", tmp_path)
    assert path == tmp_path / name
    assert path.read_bytes() == b"wheel bytes"
    assert seen == {"f": name, "s": hashlib.sha256(b"wheel bytes").hexdigest(), "p": b"{}"}


def test_bytes_that_differ_from_the_listed_hash_are_refused_unwritten(pypi, monkeypatch, tmp_path):
    _serve_wheel(pypi, "9.0.0", b"swapped bytes", listed_sha256="cd" * 32)
    monkeypatch.setattr(provenance, "verify_distribution", lambda *a: None)
    with pytest.raises(provenance.ProvenanceError) as exc:
        provenance.download_verified_wheel("9.0.0", tmp_path)
    assert exc.value.reason == "digest_mismatch"
    assert list(tmp_path.iterdir()) == []


def test_a_wheel_without_provenance_is_refused_unwritten(pypi, tmp_path):
    _serve_wheel(pypi, "9.0.0", b"wheel bytes", provenance_body=None)
    with pytest.raises(provenance.ProvenanceError) as exc:
        provenance.download_verified_wheel("9.0.0", tmp_path)
    assert exc.value.reason == "provenance_missing"
    assert not exc.value.transient
    assert list(tmp_path.iterdir()) == []


def test_a_wheel_with_a_failing_attestation_is_refused_unwritten(pypi, tmp_path):
    _serve_wheel(pypi, "9.0.0", b"wheel bytes", provenance_body=_real_provenance())
    with pytest.raises(provenance.ProvenanceError) as exc:
        provenance.download_verified_wheel("9.0.0", tmp_path)
    assert exc.value.reason == "wrong_publisher"
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# The installed files
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


_SHIPPED = {
    "booking_agent/__init__.py": b"core\n",
    "booking_agent_cli/__init__.py": b"cli\n",
    "booking_agent_cli/defaults.env": b"A=1\n",
}


def _install(root: Path) -> dict:
    for rel, data in _SHIPPED.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return {rel: _b64(data) for rel, data in _SHIPPED.items()}


def _wheel_with_record(path: Path, record: dict) -> Path:
    lines = [f"{rel},sha256={digest},1" for rel, digest in record.items()]
    lines.append("booking_agent_cli-9.0.0.dist-info/RECORD,,")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("booking_agent_cli-9.0.0.dist-info/RECORD", "\n".join(lines) + "\n")
    return path


def test_an_untouched_installation_matches_the_record(tmp_path):
    record = _install(tmp_path)
    assert provenance.compare_installed(record, tmp_path) is None


def test_the_record_is_read_from_the_wheel_without_unhashed_entries(tmp_path):
    record = {rel: _b64(data) for rel, data in _SHIPPED.items()}
    assert provenance._record_hashes(_wheel_with_record(tmp_path / "w.whl", record)) == record


@pytest.mark.parametrize("tamper, reason", [
    (lambda root: (root / "booking_agent_cli/__init__.py").write_bytes(b"evil\n"), "installed_file_modified"),
    (lambda root: (root / "booking_agent_cli/defaults.env").unlink(), "installed_file_missing"),
    (lambda root: (root / "booking_agent/hook.py").write_text("evil"), "unexpected_file"),
])
def test_a_tampered_installation_is_caught(tmp_path, tamper, reason):
    record = _install(tmp_path)
    tamper(tmp_path)
    assert provenance.compare_installed(record, tmp_path).reason == reason


def test_compiled_caches_and_other_packages_are_not_flagged(tmp_path):
    record = _install(tmp_path)
    (tmp_path / "booking_agent/__pycache__").mkdir()
    (tmp_path / "booking_agent/__pycache__/__init__.cpython-312.pyc").write_bytes(b"\0")
    (tmp_path / "httpx").mkdir()
    (tmp_path / "httpx/__init__.py").write_text("")
    assert provenance.compare_installed(record, tmp_path) is None


@pytest.fixture
def index_install(monkeypatch, tmp_path):
    root = tmp_path / "site-packages"
    root.mkdir()
    record = _install(root)
    monkeypatch.setattr(provenance, "install_source", lambda: provenance.INSTALL_INDEX)
    monkeypatch.setattr(provenance, "_installed_root", lambda: root)

    def download(version, dest_dir):
        return _wheel_with_record(Path(dest_dir) / provenance.wheel_filename(version), record)

    monkeypatch.setattr(provenance, "download_verified_wheel", download)
    return root


def test_an_index_installation_that_matches_its_release_verifies(index_install):
    assert provenance.verify_installed(version="9.0.0").ok


def test_an_index_installation_that_differs_is_tampered(index_install):
    (index_install / "booking_agent/__init__.py").write_bytes(b"evil\n")
    result = provenance.verify_installed(version="9.0.0")
    assert result.tampered
    assert result.reason == "installed_file_modified"


def test_failing_to_reach_pypi_is_not_tampering(index_install, monkeypatch):
    def offline(version, dest_dir):
        raise provenance.ProvenanceError("network", "offline", transient=True)

    monkeypatch.setattr(provenance, "download_verified_wheel", offline)
    result = provenance.verify_installed(version="9.0.0")
    assert not result.ok and result.transient and not result.tampered


def test_a_release_without_provenance_is_tampering(index_install, monkeypatch):
    def missing(version, dest_dir):
        raise provenance.ProvenanceError("provenance_missing", "none")

    monkeypatch.setattr(provenance, "download_verified_wheel", missing)
    assert provenance.verify_installed(version="9.0.0").tampered


@pytest.mark.parametrize("source", [provenance.INSTALL_EDITABLE, provenance.INSTALL_LOCAL])
def test_source_checkouts_are_skipped(monkeypatch, source):
    monkeypatch.setattr(provenance, "install_source", lambda: source)
    result = provenance.verify_installed(version="9.0.0")
    assert result.ok and result.skipped == source and not result.tampered


@pytest.mark.parametrize("direct_url, source", [
    (None, provenance.INSTALL_INDEX),
    ({"url": "file:///src/booking-agent", "dir_info": {"editable": True}}, provenance.INSTALL_EDITABLE),
    ({"url": "file:///src/booking-agent/packages/cli", "dir_info": {}}, provenance.INSTALL_LOCAL),
    ({"url": "file:///home/u/.booking-agent/verified-wheels/x.whl", "archive_info": {}}, provenance.INSTALL_INDEX),
])
def test_the_install_source_comes_from_direct_url(monkeypatch, direct_url, source):
    text = json.dumps(direct_url) if direct_url is not None else None
    monkeypatch.setattr(provenance, "_distribution", lambda: SimpleNamespace(read_text=lambda name: text))
    assert provenance.install_source() == source
