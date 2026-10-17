#!/usr/bin/env python3
"""Copy skills/booking-agent/SKILL.md into the CLI package so the wheel ships the matching skill."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "booking-agent" / "SKILL.md"
BUNDLE = ROOT / "packages" / "cli" / "src" / "booking_agent_cli" / "SKILL.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if the bundled copy differs from the skill (no write)",
    )
    args = parser.parse_args()
    data = SOURCE.read_bytes()
    if args.check:
        if not BUNDLE.is_file() or BUNDLE.read_bytes() != data:
            print(
                f"DRIFT: {BUNDLE.relative_to(ROOT)} differs from {SOURCE.relative_to(ROOT)}. "
                "Run: uv run python scripts/sync_skill_bundle.py",
                file=sys.stderr,
            )
            return 1
        print("OK: bundled skill matches")
        return 0
    BUNDLE.write_bytes(data)
    print(f"Wrote {BUNDLE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
