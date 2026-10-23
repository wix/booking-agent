#!/usr/bin/env python3
"""Verified harness install docs: generate the tables and pinned values from the registry.

``ops/verified-harnesses.json`` is the single source. This script rewrites the marked
blocks in ``README.md``, ``docs/harnesses/README.md``, and each harness's install guide,
so a pinned CLI or skill version is never typed by hand.

Usage (from ``booking-agent/``):

    python scripts/harness_docs.py            # rewrite the marked blocks
    python scripts/harness_docs.py --check    # fail when a block is out of date
"""
from __future__ import annotations

import argparse
import os
import posixpath
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_notes  # noqa: E402

WORKSPACE = release_notes.WORKSPACE
TABLE = "verified-harnesses"
PINS = "harness-pins"
TABLE_DOCS = ("README.md", release_notes.HARNESS_GUIDES + "README.md")


def _day(text: str) -> str:
    return date.fromisoformat(text).strftime("%d %b %Y").lstrip("0")


def render_table(registry: dict, doc: str) -> str:
    base = posixpath.dirname(doc) or "."
    rows = [
        "| Harness | Install guide | Approved CLI | Approved skill | Hosted replay |",
        "|---|---|---|---|---|",
    ]
    for harness in sorted(registry["harnesses"].values(), key=lambda h: h["name"].lower()):
        link = f"[Install on {harness['name']}]({posixpath.relpath(harness['guide'], base)})"
        if harness["status"] == "verified":
            pinned, ran = harness["recommended"], harness["replayed"]
            rows.append(
                f"| {harness['name']} | {link} | `{pinned['cli']}` | `{pinned['skill']}` "
                f"| Passed {_day(ran['date'])} with CLI {ran['cli']} |"
            )
        else:
            rows.append(f"| {harness['name']} | {link} | - | - | Pending |")
    return "\n".join(rows)


def render_pins(harness: dict) -> str:
    if harness["status"] != "verified":
        return (
            "This harness has not passed its replay yet, so no CLI and skill pair is approved "
            "for it. Use the general install in the README until it does."
        )
    pinned, ran = harness["recommended"], harness["replayed"]
    lines = [
        "| Item | Value |",
        "|---|---|",
        f"| Approved Booking Agent CLI | `{pinned['cli']}` |",
        f"| Approved Booking Agent skill | `{pinned['skill']}`, bundled in that CLI |",
        f"| Replay id | `{harness['replay']}` |",
        f"| Hosted replay | Passed {_day(ran['date'])} with CLI {ran['cli']} and skill {ran['skill']} |",
        "",
    ]
    if ran["cli"] != pinned["cli"]:
        lines += [
            "The approved CLI carries every change the replayed CLI ran. "
            "Its release notes record why it is approved.",
            "",
        ]
    lines += ["```bash", f"cli={pinned['cli']}", f"replay={harness['replay']}", "```"]
    return "\n".join(lines)


def replace_block(text: str, name: str, body: str, doc: str) -> str:
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise ValueError(f"{doc}: needs exactly one {start} ... {end} block")
    head, rest = text.split(start)
    _, tail = rest.split(end)
    return f"{head}{start}\n{body}\n{end}{tail}"


def rendered(registry: dict, workspace: Path = WORKSPACE) -> dict[str, str]:
    """Every generated doc, keyed by its path relative to ``workspace``."""
    docs = {}
    for doc in TABLE_DOCS:
        text = (workspace / doc).read_text(encoding="utf-8")
        docs[doc] = replace_block(text, TABLE, render_table(registry, doc), doc)
    for harness in registry["harnesses"].values():
        doc = harness["guide"]
        text = (workspace / doc).read_text(encoding="utf-8")
        docs[doc] = replace_block(text, PINS, render_pins(harness), doc)
    return docs


def check_errors(registry: dict, workspace: Path = WORKSPACE) -> list[str]:
    errors = release_notes.registry_shape_errors(registry)
    if errors:
        return errors
    missing = [h["guide"] for h in registry["harnesses"].values() if not (workspace / h["guide"]).is_file()]
    if missing:
        return [f"{doc} does not exist" for doc in missing]
    try:
        docs = rendered(registry, workspace)
    except ValueError as exc:
        return [str(exc)]
    return [
        f"{doc} is out of date: run `python scripts/harness_docs.py`"
        for doc, text in docs.items()
        if (workspace / doc).read_text(encoding="utf-8") != text
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when a generated block is out of date")
    args = parser.parse_args(argv)
    registry = release_notes.load_registry()
    errors = check_errors(registry)
    if not args.check and errors and all("is out of date" in e for e in errors):
        for doc, text in rendered(registry).items():
            (WORKSPACE / doc).write_text(text, encoding="utf-8")
            print(f"wrote {doc}")
        errors = []
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "error: "
    for error in errors:
        print(f"{prefix}{error}", file=sys.stderr)
    if not errors and args.check:
        print("verified harness docs are current")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
