#!/usr/bin/env python3
"""Release version guards for booking-agent-cli.

Every merge into master that raises the version is published to PyPI, so CI checks:

* consistency (always): the pyproject version, both ``__version__`` strings, and the
  ``booking-agent-cli`` entry in ``uv.lock`` agree;
* bump (with ``--base REF``): a change to shipped code raises the version above the
  version on ``REF``, and the version never moves backwards.

Shipped code is what the wheel contains: the packages listed under
``[tool.hatch.build.targets.wheel] packages`` (``test_wheel_contents.py`` holds the
wheel to exactly those) plus the distribution metadata in ``pyproject.toml``. The
package README, tests, docs, CI, and ``uv.lock`` do not need a bump.

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
    return errors


def shipped_prefixes(pyproject: dict, cli_rel: str) -> list[str]:
    """Repository-relative directory prefixes whose files end up in the wheel."""
    packages = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    return [f"{cli_rel}/{package.strip('/')}/" for package in packages]


def _without_version(pyproject: dict) -> dict:
    project = {k: v for k, v in pyproject.get("project", {}).items() if k != "version"}
    return {**pyproject, "project": project}


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
    return bump_errors(changed, base_pyproject, head_pyproject, cli_rel)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", help="git ref of the base branch, for example origin/master")
    args = parser.parse_args(argv)

    errors = consistency_errors()
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
