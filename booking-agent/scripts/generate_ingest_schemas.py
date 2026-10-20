#!/usr/bin/env python3
"""Write the support ingest's ``Schema.gs`` and ``FaultSchema.gs`` from the CLI schemas.

The ingest is an Apps Script project kept outside this repository. Point ``--out-dir``
(or ``USP_SPEC_INGEST_DIR``) at its ``apps-script`` folder. ``--check`` writes nothing
and fails when either file differs from what the CLI would generate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict

from booking_agent_cli.fault_report_schema import FAULT_REPORT_SCHEMA
from booking_agent_cli.support_bundle_schema import SUPPORT_BUNDLE_SCHEMA

_SOURCES = {
    "Schema.gs": (
        "SUPPORT_BUNDLE_SCHEMA",
        "packages/cli/src/booking_agent_cli/support_bundle_schema.py",
        SUPPORT_BUNDLE_SCHEMA,
    ),
    "FaultSchema.gs": (
        "FAULT_REPORT_SCHEMA",
        "packages/cli/src/booking_agent_cli/fault_report_schema.py",
        FAULT_REPORT_SCHEMA,
    ),
}


def rendered() -> Dict[str, str]:
    out = {}
    for filename, (name, source, schema) in _SOURCES.items():
        out[filename] = (
            f"// Generated from {name} in {source}; do not edit.\n"
            f"var {name} = " + json.dumps(schema, indent=2, sort_keys=True) + ";\n"
        )
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=os.environ.get("USP_SPEC_INGEST_DIR"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.out_dir:
        parser.error("pass --out-dir <ingest/apps-script folder> or set USP_SPEC_INGEST_DIR")
    out_dir = Path(args.out_dir)
    drift = []
    for filename, text in rendered().items():
        path = out_dir / filename
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != text:
                drift.append(filename)
        else:
            path.write_text(text, encoding="utf-8")
            print(f"Wrote {path}")
    if drift:
        print("Out of date: " + ", ".join(drift), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
