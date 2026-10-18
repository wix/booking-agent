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


@pytest.mark.parametrize(
    ("base", "head"),
    [
        ("0.1.0", "0.1.1"),
        ("0.1.0", "0.2.0"),
        ("0.1.0", "1.0.0"),
        ("0.1.3", "0.2.0"),
        ("1.4.2", "2.0.0"),
        ("0.1.0", "0.2.0.dev1"),
        ("0.2.0.dev1", "0.2.0.dev2"),
        ("0.2.0.dev1", "0.2.0"),
        ("0.2.0.dev1", "0.2.1"),
    ],
)
def test_one_semver_step_passes(base, head):
    assert _errors([f"{CLI}/src/booking_agent/flow.py"], base=_pyproject(base), head=_pyproject(head)) == []


@pytest.mark.parametrize(
    ("base", "head"),
    [("0.1.0", "0.1.2"), ("0.1.0", "0.3.0"), ("0.1.0", "0.2.1"), ("0.1.0", "2.0.0"), ("0.1.0", "1.0.1")],
)
def test_skipping_numbers_fails(base, head):
    (error,) = _errors([], base=_pyproject(base), head=_pyproject(head))
    assert f"skips numbers after {base}" in error


@pytest.mark.parametrize("head", ["0.2", "0.1.0.1", "0.2.0rc1", "0.1.0.post1", "0.1.1+local"])
def test_changed_version_must_be_major_minor_patch(head):
    (error,) = _errors([], head=_pyproject(head))
    assert "must be MAJOR.MINOR.PATCH" in error


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


_SKILL = """\
metadata:
  openclaw:
    install:
      - kind: uv
        package: "booking-agent-cli=={v}"
        bins: [booking-agent]
---
uvx pip download "booking-agent-cli=={v}" --no-deps -d "$dir"
uv tool install --force "$dir/booking_agent_cli-{v}-py3-none-any.whl"
"""


def test_a_skill_pinned_to_the_version_passes():
    assert check_version.skill_pin_errors(_SKILL.format(v="0.3.0"), "0.3.0") == []


def test_a_stale_pin_anywhere_in_the_skill_is_reported():
    text = _SKILL.format(v="0.3.0").replace("booking_agent_cli-0.3.0", "booking_agent_cli-0.2.0")
    errors = check_version.skill_pin_errors(text, "0.3.0")
    assert len(errors) == 1 and "0.2.0" in errors[0]


@pytest.mark.parametrize("install", [
    "      - kind: python\n        package: booking-agent-cli\n",
    '      - kind: uv\n        package: "booking-agent-cli"\n',
])
def test_an_unpinned_or_non_uv_install_block_is_reported(install):
    text = _SKILL.format(v="0.3.0").replace(
        '      - kind: uv\n        package: "booking-agent-cli==0.3.0"\n', install
    )
    assert any("kind: uv" in e for e in check_version.skill_pin_errors(text, "0.3.0"))


def test_pinning_rewrites_every_pin_and_nothing_else():
    pinned = check_version.pin_skill_text(_SKILL.format(v="0.2.0") + "booking-agent-cli>=0.1\n", "0.3.0.dev1")
    assert pinned == _SKILL.format(v="0.3.0.dev1") + "booking-agent-cli>=0.1\n"


def test_consistency_reports_a_stale_skill_pin(tmp_path):
    shutil.copytree(_WORKSPACE / "packages" / "cli" / "src", tmp_path / "packages" / "cli" / "src")
    shutil.copy(_WORKSPACE / "packages" / "cli" / "pyproject.toml", tmp_path / "packages" / "cli")
    shutil.copy(_WORKSPACE / "uv.lock", tmp_path)
    skill = tmp_path / "skills" / "booking-agent" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(_SKILL.format(v="0.0.1"))
    errors = check_version.consistency_errors(tmp_path)
    assert len(errors) == 1 and errors[0].startswith("skills/booking-agent/SKILL.md")


# ---------------------------------------------------------------------------
# The skill's own version, its layout, and the URLs that point at it
# ---------------------------------------------------------------------------

_FRONTMATTER = """\
---
version: {v}
name: booking-agent
metadata:
  url: {latest}
  openclaw:
    homepage: {latest}
---

See {latest} for the latest text.
"""

_LATEST = check_version.SKILL_LATEST_URL
_PINNED = check_version.skill_pinned_url("0.50.0")


def _skill(version: str = "0.50.0", latest: str = _LATEST) -> str:
    return _FRONTMATTER.format(v=version, latest=latest)


@pytest.mark.parametrize("text, expected", [
    (_skill("0.50.0"), "0.50.0"),
    ('---\nversion: "1.2.3"\n---\n', "1.2.3"),
    ("no frontmatter\nversion: 9.9.9\n", None),
    ("---\nname: booking-agent\n---\n", None),
])
def test_the_skill_version_comes_only_from_its_frontmatter(text, expected):
    assert check_version.skill_version(text) == expected


def test_only_the_skill_itself_may_be_named_skill_md():
    tracked = [
        "booking-agent/skills/booking-agent/SKILL.md",
        "booking-agent/packages/cli/src/booking_agent_cli/bundled_skill.md",
        "docs/notes.md",
    ]
    assert check_version.skill_layout_errors(tracked) == []


def test_a_second_skill_md_anywhere_is_reported():
    errors = check_version.skill_layout_errors([
        "booking-agent/skills/booking-agent/SKILL.md",
        "booking-agent/packages/cli/src/booking_agent_cli/SKILL.md",
    ])
    assert len(errors) == 1
    assert errors[0].startswith("booking-agent/packages/cli/src/booking_agent_cli/SKILL.md")
    assert "installers" in errors[0]


@pytest.mark.parametrize("url", [_LATEST, _PINNED])
def test_the_latest_and_the_current_pinned_url_are_allowed(url):
    assert check_version.skill_url_errors({"README.md": f"Fetch {url} first."}, "0.50.0") == []


# Assembled, not spelled out: skill_url_errors scans every tracked file, this one included.
_STALE_PIN = check_version.skill_pinned_url("0.49.0")
_CLI_TAG = _PINNED.replace("skill-v0.50.0", "v0.3.0")
_BLOB = "https://github.com/wix/booking-agent/blob/master/" + check_version.SKILL_REPO_PATH


@pytest.mark.parametrize("url", [_STALE_PIN, _CLI_TAG, _BLOB])
def test_a_stale_or_non_canonical_skill_url_is_reported(url):
    errors = check_version.skill_url_errors({"README.md": f"Fetch {url} first."}, "0.50.0")
    assert len(errors) == 1 and url in errors[0]


def test_files_recording_past_releases_keep_their_urls():
    stale = check_version.skill_pinned_url("0.1.0")
    files = {"CHANGE_LOG.md": stale, "booking-agent/release-notes/0.1.0.json": stale}
    assert check_version.skill_url_errors(files, "0.50.0") == []


def test_a_url_left_as_a_template_is_not_a_stale_pin():
    template = "https://raw.githubusercontent.com/wix/booking-agent/skill-v${skill_version}/booking-agent/skills/booking-agent/SKILL.md"
    assert check_version.skill_url_errors({"ci.yml": template}, "0.50.0") == []


def test_unrelated_skill_urls_are_left_alone():
    other = "https://github.com/stripe/link-cli/blob/main/skills/create-payment-credential/SKILL.md"
    license_url = "https://github.com/wix/booking-agent/blob/master/LICENSE"
    assert check_version.skill_url_errors({"SKILL.md": f"{other}\n{license_url}\n"}, "0.50.0") == []


def test_the_skill_metadata_points_at_the_latest_url():
    assert check_version.skill_metadata_url_errors(_skill()) == []


@pytest.mark.parametrize("latest", [_PINNED, "https://example.com/SKILL.md"])
def test_skill_metadata_pinned_or_elsewhere_is_reported(latest):
    errors = check_version.skill_metadata_url_errors(_skill(latest=latest))
    assert len(errors) == 2  # url and homepage
    assert all(check_version.SKILL_LATEST_URL in e for e in errors)


def test_pinning_rewrites_every_skill_url_and_nothing_else():
    text = f"{check_version.skill_pinned_url('0.1.0')}\n{_LATEST}\n"
    assert check_version.pin_skill_urls(text, "0.50.0") == f"{_PINNED}\n{_LATEST}\n"


_SKILL_PATH = check_version.SKILL_REPO_PATH


def test_an_unchanged_skill_needs_no_version_raise():
    assert check_version.skill_bump_errors(["docs/x.md"], _skill("0.50.0"), _skill("0.50.0")) == []


def test_a_changed_skill_must_raise_its_version():
    errors = check_version.skill_bump_errors([_SKILL_PATH], _skill("0.50.0"), _skill("0.50.0"))
    assert len(errors) == 1 and "skill-v<version>" in errors[0]


def test_a_changed_skill_with_a_raised_version_passes():
    assert check_version.skill_bump_errors([_SKILL_PATH], _skill("0.49.0"), _skill("0.50.0")) == []


def test_a_skill_version_cannot_move_backwards():
    errors = check_version.skill_bump_errors([_SKILL_PATH], _skill("0.50.0"), _skill("0.49.0"))
    assert len(errors) == 1 and "not above" in errors[0]


def test_a_new_skill_file_needs_no_base_comparison():
    assert check_version.skill_bump_errors([_SKILL_PATH], None, _skill("0.50.0")) == []


def test_a_changed_skill_without_a_version_is_reported():
    errors = check_version.skill_bump_errors([_SKILL_PATH], _skill("0.50.0"), "no frontmatter\n")
    assert len(errors) == 1 and "no frontmatter version" in errors[0]
