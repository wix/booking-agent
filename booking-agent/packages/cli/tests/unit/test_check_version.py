"""scripts/check_version.py: version consistency and the bump-on-shipped-change rule."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

_WORKSPACE = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("check_version", _WORKSPACE / "scripts" / "check_version.py")
check_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_version)

CLI = "booking-agent/packages/cli"
PYPROJECT = """\
[project]
name = "booking-agent-cli"
version = "{version}"
dependencies = [{deps}]

[tool.hatch.build.targets.wheel]
packages = ["src/booking_agent", "src/booking_agent_cli"]
"""


def _pyproject(version: str = "0.1.0", deps: str = '"httpx>=0.27"') -> str:
    return PYPROJECT.format(version=version, deps=deps)


def _errors(changed, base=None, head=None):
    return check_version.bump_errors(changed, base or _pyproject(), head or _pyproject(), CLI)


def test_the_repository_versions_agree():
    assert check_version.consistency_errors() == []


def test_shipped_scope_comes_from_the_wheel_packages():
    pyproject = check_version.tomllib.loads((_WORKSPACE / "packages" / "cli" / "pyproject.toml").read_text())
    assert check_version.shipped_prefixes(pyproject, CLI) == [
        f"{CLI}/src/booking_agent/",
        f"{CLI}/src/booking_agent_cli/",
    ]


@pytest.mark.parametrize(
    "changed",
    [
        ["README.md", "AGENTS.md", ".github/workflows/ci.yml"],
        [f"{CLI}/tests/unit/test_x.py", f"{CLI}/README.md", "booking-agent/uv.lock"],
        ["booking-agent/scripts/check_version.py", "booking-agent/skills/booking-agent/SKILL.md"],
        [],
    ],
)
def test_non_shipped_changes_need_no_bump(changed):
    assert _errors(changed) == []


@pytest.mark.parametrize(
    "path",
    [f"{CLI}/src/booking_agent/flow.py", f"{CLI}/src/booking_agent_cli/main.py", f"{CLI}/src/booking_agent/defaults.env"],
)
def test_shipped_change_without_bump_fails(path):
    (error,) = _errors(["README.md", path])
    assert "keeps version 0.1.0" in error and path in error


def test_shipped_change_with_bump_passes():
    assert _errors([f"{CLI}/src/booking_agent/flow.py"], head=_pyproject("0.1.1")) == []


def test_dependency_change_without_bump_fails():
    (error,) = _errors([f"{CLI}/pyproject.toml"], head=_pyproject(deps='"httpx>=0.28"'))
    assert "pyproject.toml (distribution metadata)" in error


def test_pyproject_comment_only_change_needs_no_bump():
    assert _errors([f"{CLI}/pyproject.toml"], head="# a comment\n" + _pyproject()) == []


def test_version_cannot_move_backwards():
    (error,) = _errors([], base=_pyproject("0.2.0"), head=_pyproject("0.1.9"))
    assert "lower than the base version 0.2.0" in error


def test_versions_compare_by_pep440_not_text():
    assert _errors([f"{CLI}/src/booking_agent/flow.py"], base=_pyproject("0.9.0"), head=_pyproject("0.10.0")) == []


def test_consistency_reports_each_mismatch(tmp_path):
    cli = tmp_path / "packages" / "cli"
    for module in check_version.VERSION_MODULES:
        (cli / module).parent.mkdir(parents=True, exist_ok=True)
        (cli / module).write_text('__version__ = "0.1.0"\n')
    (cli / "pyproject.toml").write_text(_pyproject("0.2.0"))
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "booking-agent-cli"\nversion = "0.1.0"\n')
    errors = check_version.consistency_errors(tmp_path)
    assert len(errors) == 3
    assert any("uv.lock" in e for e in errors)


def test_consistency_passes_for_a_real_copy(tmp_path):
    shutil.copytree(_WORKSPACE / "packages" / "cli" / "src", tmp_path / "packages" / "cli" / "src")
    shutil.copy(_WORKSPACE / "packages" / "cli" / "pyproject.toml", tmp_path / "packages" / "cli")
    shutil.copy(_WORKSPACE / "uv.lock", tmp_path)
    assert check_version.consistency_errors(tmp_path) == []
