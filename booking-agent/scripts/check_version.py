#!/usr/bin/env python3
"""Release version guards for booking-agent-cli.

Every merge into master that raises the version is published to PyPI, so CI checks:

* consistency (always): the pyproject version, both ``__version__`` strings, the
  ``booking-agent-cli`` entry in ``uv.lock``, and every ``booking-agent-cli`` pin in the
  skill (its ``kind: uv`` install metadata and its install commands) agree;
* bump (with ``--base REF``): a change to shipped code raises the version above the
  version on ``REF``, the version never moves backwards, and a changed version is
  ``MAJOR.MINOR.PATCH`` (optionally ``.devN``) one semantic-versioning step after the
  base: the next patch, the next minor, or the next major, never skipping numbers.
  Moving forward within the same release (``0.2.0.dev1`` to ``0.2.0``) is allowed.

Shipped code is what the wheel contains: the packages listed under
``[tool.hatch.build.targets.wheel] packages`` (``test_wheel_contents.py`` holds the
wheel to exactly those) plus the distribution metadata in ``pyproject.toml``. The
package README, tests, docs, CI, and ``uv.lock`` do not need a bump.

The skill is released as ``skill-v<version>`` (its frontmatter ``version``), so CI also checks:

* layout (always): ``skills/booking-agent/SKILL.md`` is the only tracked file named
  ``SKILL.md``; skill installers would install any other directory holding one instead;
* URLs (always): ``metadata.url`` and ``metadata.openclaw.homepage`` are the latest URL, and
  every other skill URL in the repository is the latest URL or the pinned URL of the
  current skill version;
* skill bump (with ``--base REF``): a changed skill raises its frontmatter ``version``,
  because a released ``skill-v<version>`` tag can never move.

Usage (from anywhere in the repository):

    python booking-agent/scripts/check_version.py                     # consistency only
    python booking-agent/scripts/check_version.py --base origin/master
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from packaging.version import InvalidVersion, Version

PACKAGE = "booking-agent-cli"
WORKSPACE = Path(__file__).resolve().parents[1]
CLI_DIR = WORKSPACE / "packages" / "cli"
VERSION_MODULES = (
    Path("src/booking_agent/__init__.py"),
    Path("src/booking_agent_cli/__init__.py"),
)

_DUNDER_VERSION = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
# The skill and its bundled copy pin the CLI release they were written for.
SKILL_SOURCE = Path("skills/booking-agent/SKILL.md")
SKILL_FILES = (
    SKILL_SOURCE,
    Path("packages/cli/src/booking_agent_cli/bundled_skill.md"),
)
SKILL_REPO_PATH = "booking-agent/skills/booking-agent/SKILL.md"
SKILL_LATEST_URL = f"https://raw.githubusercontent.com/wix/booking-agent/master/{SKILL_REPO_PATH}"
# Files that record past releases keep the URLs they were written with.
SKILL_URL_HISTORY = ("CHANGE_LOG.md", "booking-agent/release-notes/")
_URL_CHARS = r"[^\s)\"'<>\]`|]"
_SKILL_URL = re.compile(
    rf"https?://{_URL_CHARS}*wix/booking-agent{_URL_CHARS}*?(?:SKILL\.md|skills/booking-agent){_URL_CHARS}*"
)
_SKILL_PINNED_URL = re.compile(
    r"(https://raw\.githubusercontent\.com/wix/booking-agent/skill-v)([^/\s]+)(/booking-agent/skills/booking-agent/SKILL\.md)"
)
_FRONTMATTER_VERSION = re.compile(r"^version:\s*[\"']?([^\s\"']+)[\"']?\s*$", re.MULTILINE)
_FRONTMATTER_URL = re.compile(r"^\s*(url|homepage):\s*(\S+)\s*$", re.MULTILINE)
_SKILL_PIN = re.compile(r"(booking-agent-cli==)([0-9][0-9A-Za-z.]*[0-9A-Za-z])")
_SKILL_WHEEL_PIN = re.compile(r"(booking_agent_cli-)([0-9][0-9A-Za-z.]*)(-py3-none-any\.whl)")
_SKILL_INSTALL = re.compile(
    r'^\s+- kind: uv\n\s+package: "booking-agent-cli==[^"]+"\n\s+bins: \[booking-agent\]$', re.MULTILINE
)


def lock_version(lock_text: str, package: str = PACKAGE) -> str | None:
    for entry in tomllib.loads(lock_text).get("package", []):
        if entry.get("name") == package:
            return entry.get("version")
    return None


def consistency_errors(workspace: Path = WORKSPACE) -> list[str]:
    cli = workspace / "packages" / "cli"
    version = tomllib.loads((cli / "pyproject.toml").read_text())["project"]["version"]
    errors = []
    for module in VERSION_MODULES:
        match = _DUNDER_VERSION.search((cli / module).read_text())
        found = match.group(1) if match else None
        if found != version:
            errors.append(f"{cli.name}/{module} has __version__ {found!r}, pyproject.toml has {version!r}")
    locked = lock_version((workspace / "uv.lock").read_text())
    if locked != version:
        errors.append(f"uv.lock pins {PACKAGE} {locked!r}, pyproject.toml has {version!r} (run `uv lock`)")
    for skill in SKILL_FILES:
        path = workspace / skill
        if path.is_file():
            errors += [f"{skill}: {e}" for e in skill_pin_errors(path.read_text(encoding="utf-8"), version)]
    return errors


def pin_skill_text(text: str, version: str) -> str:
    """``text`` with every booking-agent-cli version pin set to ``version``."""
    text = _SKILL_PIN.sub(lambda m: f"{m.group(1)}{version}", text)
    return _SKILL_WHEEL_PIN.sub(lambda m: f"{m.group(1)}{version}{m.group(3)}", text)


def skill_pin_errors(text: str, version: str) -> list[str]:
    """The skill installs exactly ``version`` through uv, and every pin in it names ``version``."""
    errors = []
    if not _SKILL_INSTALL.search(text):
        errors.append(
            f'metadata.openclaw.install must be `kind: uv` with `package: "{PACKAGE}=={version}"`'
        )
    stale = sorted({m.group(2) for m in _SKILL_PIN.finditer(text)} | {m.group(2) for m in _SKILL_WHEEL_PIN.finditer(text)})
    stale = [v for v in stale if v != version]
    if stale:
        errors.append(
            f"pins {PACKAGE} {', '.join(stale)}, pyproject.toml has {version!r} "
            "(run `uv run python scripts/sync_skill_bundle.py`)"
        )
    return errors


def skill_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    return text[4:end] if end != -1 else ""


def skill_version(text: str) -> str | None:
    """The skill's own frontmatter ``version`` (not the CLI it pins)."""
    match = _FRONTMATTER_VERSION.search(skill_frontmatter(text))
    return match.group(1) if match else None


def skill_pinned_url(version: str) -> str:
    return f"https://raw.githubusercontent.com/wix/booking-agent/skill-v{version}/{SKILL_REPO_PATH}"


def pin_skill_urls(text: str, version: str) -> str:
    """``text`` with every pinned skill URL set to ``skill-v<version>``."""
    return _SKILL_PINNED_URL.sub(lambda m: f"{m.group(1)}{version}{m.group(3)}", text)


def _is_url_template(url: str) -> bool:
    match = _SKILL_PINNED_URL.fullmatch(url)
    return bool(match) and re.fullmatch(r"\$?\{[^}]+\}", match.group(2)) is not None


def skill_layout_errors(tracked: list[str]) -> list[str]:
    """Only the skill itself is named SKILL.md (``tracked`` is repository-relative)."""
    return [
        f"{path}: only {SKILL_REPO_PATH} may be named SKILL.md. Skill installers "
        "(npx skills, harness scanners) treat any directory holding a SKILL.md as the skill "
        "and would install that directory instead; give the copy another name"
        for path in sorted(tracked)
        if path.rsplit("/", 1)[-1] == "SKILL.md" and path != SKILL_REPO_PATH
    ]


def skill_url_errors(files: dict[str, str], version: str) -> list[str]:
    """Every skill URL in ``files`` (repository-relative path -> text) is canonical."""
    allowed = {SKILL_LATEST_URL, skill_pinned_url(version)}
    errors = []
    for path, text in sorted(files.items()):
        if path.startswith(SKILL_URL_HISTORY):
            continue
        urls = {m.group(0).rstrip(".,:;") for m in _SKILL_URL.finditer(text)}
        for url in sorted(u for u in urls if u not in allowed and not _is_url_template(u)):
            errors.append(
                f"{path}: skill URL {url} is not canonical. Use {SKILL_LATEST_URL} for the latest "
                f"skill, or {skill_pinned_url(version)} with the `gh attestation verify` command "
                "for this exact text (run `uv run python scripts/sync_skill_bundle.py`)"
            )
    return errors


def skill_metadata_url_errors(text: str) -> list[str]:
    found = _FRONTMATTER_URL.findall(skill_frontmatter(text))
    errors = []
    for key in ("url", "homepage"):
        values = [value for k, value in found if k == key]
        if values != [SKILL_LATEST_URL]:
            errors.append(f"{SKILL_REPO_PATH}: metadata {key} must be {SKILL_LATEST_URL}, found {values or 'none'}")
    return errors


def skill_bump_errors(changed_files: list[str], base_skill: str | None, head_skill: str) -> list[str]:
    """A changed skill raises its frontmatter version, since ``skill-v<version>`` never moves."""
    if SKILL_REPO_PATH not in changed_files or base_skill is None:
        return []
    base, head = skill_version(base_skill), skill_version(head_skill)
    if head is None:
        return [f"{SKILL_REPO_PATH} has no frontmatter version"]
    if base is None:
        return []
    try:
        raised = Version(head) > Version(base)
    except InvalidVersion as exc:
        return [f"invalid skill version in {SKILL_REPO_PATH}: {exc}"]
    if raised:
        return []
    return [
        f"{SKILL_REPO_PATH} changed but its frontmatter version {head} is not above {base}. Raise "
        "`version` in the skill frontmatter: each skill version is released once as the protected "
        "tag skill-v<version>, and a CLI version bump changes the skill's install pins"
    ]


def shipped_prefixes(pyproject: dict, cli_rel: str) -> list[str]:
    """Repository-relative directory prefixes whose files end up in the wheel."""
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    return [f"{cli_rel}/{package.strip('/')}/" for package in packages]


def _without_version(pyproject: dict) -> dict:
    project = {k: v for k, v in pyproject.get("project", {}).items() if k != "version"}
    return {**pyproject, "project": project}


def next_releases(base: Version) -> list[tuple[int, int, int]]:
    """The patch, minor, and major releases that may follow ``base``."""
    major, minor, patch = (tuple(base.release) + (0, 0, 0))[:3]
    return [(major, minor, patch + 1), (major, minor + 1, 0), (major + 1, 0, 0)]


def step_errors(base: Version, head: Version) -> list[str]:
    if head == base:
        return []
    if len(head.release) != 3 or head.local or head.post is not None or head.pre is not None:
        return [f"version {head} must be MAJOR.MINOR.PATCH, optionally with a .devN suffix"]
    if head.release == (tuple(base.release) + (0, 0, 0))[:3] or head.release in next_releases(base):
        return []
    allowed = ", ".join(".".join(map(str, r)) for r in next_releases(base))
    return [f"version {head} skips numbers after {base}. Use one of {allowed} (optionally with .devN)"]


def bump_errors(
    changed_files: list[str], base_pyproject: str, head_pyproject: str, cli_rel: str
) -> list[str]:
    base, head = tomllib.loads(base_pyproject), tomllib.loads(head_pyproject)
    try:
        base_version = Version(base["project"]["version"])
        head_version = Version(head["project"]["version"])
    except InvalidVersion as exc:
        return [f"invalid version in pyproject.toml: {exc}"]

    prefixes = shipped_prefixes(head, cli_rel) + shipped_prefixes(base, cli_rel)
    shipped = sorted({f for f in changed_files if f.startswith(tuple(prefixes))})
    if _without_version(base) != _without_version(head):
        shipped.append(f"{cli_rel}/pyproject.toml (distribution metadata)")

    if head_version < base_version:
        return [f"version {head_version} is lower than the base version {base_version}"]
    if errors := step_errors(base_version, head_version):
        return errors
    if shipped and head_version == base_version:
        listed = "\n".join(f"  {f}" for f in shipped)
        return [
            f"this change ships new code but keeps version {base_version}. Raise `version` in "
            f"{cli_rel}/pyproject.toml above {base_version}, update both __version__ strings, "
            f"and run `uv lock`. Every merged version is published to PyPI. Shipped files changed:\n{listed}"
        ]
    return []


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=WORKSPACE, check=True, capture_output=True, text=True).stdout


def git_bump_errors(base: str) -> list[str]:
    top = Path(_git("rev-parse", "--show-toplevel").strip())
    cli_rel = CLI_DIR.relative_to(top).as_posix()
    changed = _git("diff", "--name-only", f"{base}...HEAD").splitlines()
    base_pyproject = _git("show", f"{base}:{cli_rel}/pyproject.toml")
    head_pyproject = _git("show", f"HEAD:{cli_rel}/pyproject.toml")
    try:
        base_skill = _git("show", f"{base}:{SKILL_REPO_PATH}")
    except subprocess.CalledProcessError:
        base_skill = None
    head_skill = (top / SKILL_REPO_PATH).read_text(encoding="utf-8")
    return bump_errors(changed, base_pyproject, head_pyproject, cli_rel) + skill_bump_errors(
        changed, base_skill, head_skill
    )


def repository_skill_errors() -> list[str]:
    top = Path(_git("rev-parse", "--show-toplevel").strip())
    tracked = _git("-C", str(top), "ls-files").splitlines()
    skill = (top / SKILL_REPO_PATH).read_text(encoding="utf-8")
    version = skill_version(skill)
    if version is None:
        return [f"{SKILL_REPO_PATH} has no frontmatter version"]
    files = {}
    for path in tracked:
        try:
            files[path] = (top / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return skill_layout_errors(tracked) + skill_metadata_url_errors(skill) + skill_url_errors(files, version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="git ref of the base branch, for example origin/master")
    args = parser.parse_args(argv)

    errors = consistency_errors() + repository_skill_errors()
    if args.base:
        errors += git_bump_errors(args.base)
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "error: "
    for error in errors:
        print(f"{prefix}{error}", file=sys.stderr)
    if not errors:
        print("version checks passed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
