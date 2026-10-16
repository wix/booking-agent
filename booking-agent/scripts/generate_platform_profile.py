#!/usr/bin/env python3
"""Emit the repository-root platform-profile.json from platform_profile_doc()."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "cli" / "src"))

from booking_agent.platform_profile import platform_profile_doc  # noqa: E402

# GitHub Pages serves the file at the repository root, one level above booking-agent/.
DEFAULT_OUT = ROOT.parent / "platform-profile.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if output would differ from the existing file (no write)",
    )
    args = parser.parse_args()
    doc = platform_profile_doc()
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.output.is_file():
            print(f"MISSING: {args.output}", file=sys.stderr)
            return 1
        existing = args.output.read_text(encoding="utf-8")
        if existing != text:
            print(f"DRIFT: {args.output} differs from platform_profile_doc()", file=sys.stderr)
            return 1
        print(f"OK: {args.output} matches platform_profile_doc()")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
