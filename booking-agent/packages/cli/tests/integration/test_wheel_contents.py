"""The published wheel holds product code and its package data, nothing else."""
from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

_WORKSPACE = Path(__file__).resolve().parents[4]

# Top-level import package -> package data files it must ship.
_PACKAGES = {
    "booking_agent": {"defaults.env"},
    "booking_agent_cli": set(),
}


def test_wheel_contains_only_src_and_package_data(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is needed to build wheels")
    subprocess.run(
        ["uv", "build", "--wheel", "--package", "booking-agent-cli", "--out-dir", str(tmp_path)],
        cwd=_WORKSPACE, check=True, capture_output=True, text=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()

    def _allowed(name: str) -> bool:
        top, _, rest = name.partition("/")
        if top.endswith(".dist-info"):
            return True
        return top in _PACKAGES and (rest.endswith(".py") or rest.rsplit("/", 1)[-1] in _PACKAGES[top])

    assert [n for n in names if not _allowed(n)] == []
    for top, data_files in _PACKAGES.items():
        assert any(n.startswith(f"{top}/") for n in names), top
        for data in data_files:
            assert f"{top}/{data}" in names, data
