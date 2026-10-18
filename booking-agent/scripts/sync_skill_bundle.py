#!/usr/bin/env python3
"""Copy skills/booking-agent/SKILL.md into the CLI package so the wheel ships the matching skill.

Before copying, two kinds of pin in the skill are brought up to date:

* every ``booking-agent-cli`` pin (the ``kind: uv`` install metadata and the install
  commands) is set to the version in ``packages/cli/pyproject.toml``;
* every ``skill-v<version>`` URL, here and in the two READMEs, is set to the skill's own
  frontmatter ``version`` -- the tag that release publishes the signed skill under.

The two versions move independently, which is why each has its own source.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_version import (  # noqa: E402
    pin_skill_text,
    pin_skill_urls,
    skill_pin_errors,
    skill_version,
    tomllib,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "booking-agent" / "SKILL.md"
BUNDLE = ROOT / "packages" / "cli" / "src" / "booking_agent_cli" / "bundled_skill.md"
PYPROJECT = ROOT / "packages" / "cli" / "pyproject.toml"
# Every file that links the skill at its released tag.
URL_FILES = (SOURCE, ROOT / "README.md", ROOT / "packages" / "cli" / "README.md")


def cli_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def _pinned(path: Path, text: str, version: str, skill: str) -> str:
    """``text`` with its skill URLs pinned, and its CLI pins too when it is the skill."""
    text = pin_skill_urls(text, skill)
    return pin_skill_text(text, version) if path == SOURCE else text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if a pin, a skill URL, or the bundled copy is out of date (no write)",
    )
    args = parser.parse_args()
    version = cli_version()
    text = SOURCE.read_text(encoding="utf-8")
    skill = skill_version(text)
    if skill is None:
        print(f"DRIFT: {SOURCE.relative_to(ROOT)} has no frontmatter version", file=sys.stderr)
        return 1
    texts = {p: (text if p == SOURCE else p.read_text(encoding="utf-8")) for p in URL_FILES}
    if args.check:
        problems = [f"{SOURCE.relative_to(ROOT)}: {e}" for e in skill_pin_errors(text, version)]
        problems += [
            f"{p.relative_to(ROOT)}: a skill URL is pinned to another version, not skill-v{skill}"
            for p, t in texts.items()
            if pin_skill_urls(t, skill) != t
        ]
        if not BUNDLE.is_file() or BUNDLE.read_bytes() != SOURCE.read_bytes():
            problems.append(f"{BUNDLE.relative_to(ROOT)} differs from {SOURCE.relative_to(ROOT)}")
        if problems:
            for problem in problems:
                print(f"DRIFT: {problem}", file=sys.stderr)
            print("Run: uv run python scripts/sync_skill_bundle.py", file=sys.stderr)
            return 1
        print(f"OK: bundled skill matches, pins booking-agent-cli {version}, links skill-v{skill}")
        return 0
    for path, original in texts.items():
        pinned = _pinned(path, original, version, skill)
        if pinned != original:
            path.write_bytes(pinned.encode("utf-8"))
            print(f"Pinned {path.relative_to(ROOT)} to booking-agent-cli=={version} / skill-v{skill}")
    BUNDLE.write_bytes(SOURCE.read_bytes())
    print(f"Wrote {BUNDLE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
