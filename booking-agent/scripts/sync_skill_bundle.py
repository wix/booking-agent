#!/usr/bin/env python3
"""Copy skills/booking-agent/SKILL.md into the CLI package so the wheel ships the matching skill.

Before copying, every ``booking-agent-cli`` pin in the skill (the ``kind: uv`` install
metadata and the install commands) is set to the version in ``packages/cli/pyproject.toml``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_version import pin_skill_text, skill_pin_errors, tomllib  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "booking-agent" / "SKILL.md"
BUNDLE = ROOT / "packages" / "cli" / "src" / "booking_agent_cli" / "SKILL.md"
PYPROJECT = ROOT / "packages" / "cli" / "pyproject.toml"


def cli_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the skill's pin or the bundled copy is out of date (no write)",
    )
    args = parser.parse_args()
    version = cli_version()
    text = SOURCE.read_bytes().decode("utf-8")
    if args.check:
        problems = [f"{SOURCE.relative_to(ROOT)}: {e}" for e in skill_pin_errors(text, version)]
        if not BUNDLE.is_file() or BUNDLE.read_bytes() != SOURCE.read_bytes():
            problems.append(f"{BUNDLE.relative_to(ROOT)} differs from {SOURCE.relative_to(ROOT)}")
        if problems:
            for problem in problems:
                print(f"DRIFT: {problem}", file=sys.stderr)
            print("Run: uv run python scripts/sync_skill_bundle.py", file=sys.stderr)
            return 1
        print("OK: bundled skill matches and pins booking-agent-cli " + version)
        return 0
    pinned = pin_skill_text(text, version)
    if pinned != text:
        SOURCE.write_bytes(pinned.encode("utf-8"))
        print(f"Pinned booking-agent-cli=={version} in {SOURCE.relative_to(ROOT)}")
    BUNDLE.write_bytes(SOURCE.read_bytes())
    print(f"Wrote {BUNDLE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
