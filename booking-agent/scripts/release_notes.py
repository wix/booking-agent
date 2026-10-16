#!/usr/bin/env python3
"""Release metadata for booking-agent-cli: validate, index, and check pull requests.

Every published version has ``release-notes/<version>.json``. It is what an installed
CLI reads to decide whether to tell the user about a newer version, so it states the
kind of change and whether businesses and the registry are ready for it. Generated
GitHub Release notes and ``CHANGE_LOG.md`` explain a release; this file decides it.

``release-notes/index.json`` holds every entry, sorted by version, and is the single
file the CLI fetches. It is generated; never edit it by hand.

Usage (from ``booking-agent/``):

    python scripts/release_notes.py              # rewrite index.json
    python scripts/release_notes.py --check      # entries valid, index current, current version covered
    python scripts/release_notes.py pr --base origin/master --title ... --labels ... --body-file ...
"""
from __future__ import annotations

import argparse
import json
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

WORKSPACE = Path(__file__).resolve().parents[1]
NOTES_DIR = WORKSPACE / "release-notes"
INDEX = NOTES_DIR / "index.json"
PYPROJECT = WORKSPACE / "packages" / "cli" / "pyproject.toml"
SKILL = WORKSPACE / "skills" / "booking-agent" / "SKILL.md"
RELEASES_URL = "https://github.com/wix/booking-agent/releases/tag/v{version}"

SCHEMA_VERSION = 1
CHANGE_TYPES = ("fix", "feature", "breaking")
UPDATE_STATUSES = ("available", "recommended", "urgent", "deferred", "withdrawn")
CAPABILITIES = ("search", "sign_in", "calendar", "booking", "payment", "cancel", "reschedule")
RELEASE_LABELS = CHANGE_TYPES + ("internal",)

REQUIRED = {
    "schema_version": int,
    "cli_version": str,
    "skill_version": str,
    "change_type": str,
    "update_status": str,
    "ecosystem_ready": bool,
    "relevant_capabilities": list,
    "user_summary": str,
    "details_url": str,
    "readiness_evidence": list,
}
OPTIONAL = {"replacement_version": str}

ROLLOUT_HEADING = "## Release and rollout"
ROLLOUT_FIELDS = (
    "Version",
    "Change type",
    "Update status",
    "Affected capabilities",
    "Interoperability impact",
    "Readiness evidence",
)
_PLACEHOLDER = re.compile(r"^(?:|-|n/?a|tbd|todo|<[^>]*>)$", re.IGNORECASE)

_SUMMARY_FORBIDDEN = re.compile(r"`|--|#\d|https?://|booking-agent |\bcli\b|\bflag\b", re.IGNORECASE)
_TITLE_MECHANICAL = re.compile(
    r"^(?:bump|release|sync|merge|wip|chore|v?\d)\b|^[a-z]+(?:\([^)]*\))?!?:", re.IGNORECASE
)
_TITLE_FILE = re.compile(r"\b[\w./-]+\.(?:py|md|toml|ya?ml|json|lock|sh)\b")
_TITLE_VERSION = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")
_TITLE_REF = re.compile(r"#\d+")


def _release(version: Version) -> tuple[int, int, int]:
    return (tuple(version.release) + (0, 0, 0))[:3]


def entry_errors(entry: object, name: str = "entry") -> list[str]:
    """Shape and self-consistency of one entry."""
    if not isinstance(entry, dict):
        return [f"{name}: must be a JSON object"]
    errors = []
    for key, kind in REQUIRED.items():
        if key not in entry:
            errors.append(f"{name}: missing {key}")
        elif not isinstance(entry[key], kind) or (kind is int and isinstance(entry[key], bool)):
            errors.append(f"{name}: {key} must be {kind.__name__}")
    for key, kind in OPTIONAL.items():
        if key in entry and not isinstance(entry[key], kind):
            errors.append(f"{name}: {key} must be {kind.__name__}")
    unknown = sorted(set(entry) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        errors.append(f"{name}: unknown keys {unknown}")
    if errors:
        return errors

    if entry["schema_version"] != SCHEMA_VERSION:
        errors.append(f"{name}: schema_version must be {SCHEMA_VERSION}")
    for key in ("cli_version", "skill_version"):
        try:
            Version(entry[key])
        except InvalidVersion:
            errors.append(f"{name}: {key} {entry[key]!r} is not a version")
    if entry["change_type"] not in CHANGE_TYPES:
        errors.append(f"{name}: change_type must be one of {list(CHANGE_TYPES)}")
    status = entry["update_status"]
    if status not in UPDATE_STATUSES:
        errors.append(f"{name}: update_status must be one of {list(UPDATE_STATUSES)}")
    bad_caps = [c for c in entry["relevant_capabilities"] if c not in CAPABILITIES]
    if bad_caps:
        errors.append(f"{name}: relevant_capabilities {bad_caps} not in {list(CAPABILITIES)}")
    evidence = [e for e in entry["readiness_evidence"] if isinstance(e, str) and e.strip()]
    if len(evidence) != len(entry["readiness_evidence"]):
        errors.append(f"{name}: readiness_evidence must be non-empty strings")

    if not entry["ecosystem_ready"] and status not in ("deferred", "withdrawn"):
        errors.append(f"{name}: ecosystem_ready false allows only update_status deferred (or withdrawn)")
    if entry["ecosystem_ready"] and not evidence:
        errors.append(f"{name}: ecosystem_ready true needs readiness_evidence")
    if status in ("recommended", "urgent") and not (entry["ecosystem_ready"] and evidence):
        errors.append(f"{name}: {status} needs ecosystem_ready true and readiness_evidence")
    if status == "withdrawn" and "replacement_version" not in entry:
        errors.append(f"{name}: withdrawn needs replacement_version")
    if status != "withdrawn" and "replacement_version" in entry:
        errors.append(f"{name}: replacement_version is only for withdrawn releases")

    summary = entry["user_summary"].strip()
    if not summary or not summary.endswith(".") or summary.count(". ") or len(summary) > 160:
        errors.append(f"{name}: user_summary must be one plain sentence of at most 160 characters")
    if _SUMMARY_FORBIDDEN.search(summary):
        errors.append(f"{name}: user_summary must not name commands, flags, links, or issue numbers")
    if entry["details_url"] != RELEASES_URL.format(version=entry["cli_version"]):
        errors.append(f"{name}: details_url must be {RELEASES_URL.format(version=entry['cli_version'])}")
    return errors


def series_errors(entries: list[dict]) -> list[str]:
    """Rules that compare entries: semver kind per step, and withdrawn replacements."""
    errors = []
    ordered = sorted(entries, key=lambda e: Version(e["cli_version"]))
    by_version = {e["cli_version"]: e for e in ordered}
    if len(by_version) != len(ordered):
        errors.append("two entries share a cli_version")
    for previous, entry in zip(ordered, ordered[1:]):
        before, after = _release(Version(previous["cli_version"])), _release(Version(entry["cli_version"]))
        if after == before:
            continue
        kind = entry["change_type"]
        name = entry["cli_version"]
        if after[0] != before[0]:
            if kind != "breaking":
                errors.append(f"{name}: a major release is change_type breaking")
        elif after[1] != before[1]:
            if before[0] >= 1 and kind != "feature":
                errors.append(f"{name}: from 1.0.0 a minor release is change_type feature")
            elif kind == "fix":
                errors.append(f"{name}: a minor release is change_type feature or breaking")
        elif kind != "fix":
            errors.append(f"{name}: a patch release is change_type fix")
    for entry in ordered:
        if entry["update_status"] != "withdrawn":
            continue
        name = entry["cli_version"]
        replacement = by_version.get(entry["replacement_version"])
        if replacement is None:
            errors.append(f"{name}: replacement_version {entry['replacement_version']} has no entry")
        elif Version(replacement["cli_version"]) <= Version(name):
            errors.append(f"{name}: replacement_version must be newer")
        elif replacement["update_status"] in ("deferred", "withdrawn"):
            errors.append(f"{name}: replacement_version must be installable, not {replacement['update_status']}")
    return errors


def load_entries(notes_dir: Path = NOTES_DIR) -> tuple[list[dict], list[str]]:
    entries, errors = [], []
    for path in sorted(notes_dir.glob("*.json")):
        if path.name == INDEX.name:
            continue
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"{path.name}: invalid JSON ({exc})")
            continue
        problems = entry_errors(entry, path.name)
        if not problems and path.stem != entry["cli_version"]:
            problems.append(f"{path.name}: file name must be {entry['cli_version']}.json")
        errors += problems
        if not problems:
            entries.append(entry)
    if not errors:
        errors += series_errors(entries)
    return entries, errors


def render_index(entries: list[dict]) -> str:
    ordered = sorted(entries, key=lambda e: Version(e["cli_version"]))
    doc = {"schema_version": SCHEMA_VERSION, "releases": ordered}
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def package_version(pyproject_text: str) -> str:
    return tomllib.loads(pyproject_text)["project"]["version"]


def skill_version(skill_text: str) -> str | None:
    head = skill_text.partition("\n---\n")[0]
    match = re.search(r"(?m)^version:\s*(\S+)\s*$", head)
    return match.group(1) if match else None


def check_errors(notes_dir: Path = NOTES_DIR, pyproject: Path = PYPROJECT) -> list[str]:
    entries, errors = load_entries(notes_dir)
    if errors:
        return errors
    version = package_version(pyproject.read_text())
    if not Version(version).is_devrelease and version not in {e["cli_version"] for e in entries}:
        errors.append(
            f"release-notes/{version}.json is missing: every published version needs release metadata"
        )
    index = notes_dir / INDEX.name
    if not index.is_file() or index.read_text(encoding="utf-8") != render_index(entries):
        errors.append("release-notes/index.json is out of date: run `python scripts/release_notes.py`")
    return errors


def _rollout_section(body: str) -> dict[str, str] | None:
    start = body.find(ROLLOUT_HEADING)
    if start < 0:
        return None
    section = body[start + len(ROLLOUT_HEADING):]
    end = re.search(r"(?m)^## ", section)
    section = section[: end.start()] if end else section
    found = {}
    for line in section.splitlines():
        match = re.match(r"^\s*[-*]?\s*\**([A-Za-z -]+?)\**\s*:\s*(.*)$", line)
        if match:
            found[match.group(1).strip().lower()] = match.group(2).strip().strip("`")
    return found


def title_errors(title: str) -> list[str]:
    title = title.strip()
    problems = []
    if not 10 <= len(title) <= 90:
        problems.append("is 10 to 90 characters")
    if not title[:1].isupper():
        problems.append("starts with a capital letter")
    if _TITLE_MECHANICAL.search(title):
        problems.append("describes the outcome, not a bump, sync, merge, or a `type:` prefix")
    if _TITLE_FILE.search(title):
        problems.append("names no files")
    if _TITLE_VERSION.search(title):
        problems.append("carries no version number (the release already has one)")
    if _TITLE_REF.search(title):
        problems.append("cites no issue or PR number")
    if not problems:
        return []
    return [
        f"title {title!r} becomes the GitHub Release note for this version. A release title "
        + "; ".join(problems)
        + ". Example: 'Recover safely when payment confirmation times out'."
    ]


def pr_errors(
    *,
    title: str,
    labels: list[str],
    body: str,
    base_version: str,
    head_version: str,
    entries: list[dict],
    skill_frontmatter_version: str | None,
) -> list[str]:
    errors = []
    release_labels = [label for label in labels if label in RELEASE_LABELS]
    if len(release_labels) != 1:
        errors.append(
            f"apply exactly one release label {list(RELEASE_LABELS)}; found {release_labels or 'none'}"
        )
    label = release_labels[0] if len(release_labels) == 1 else None
    bumped = head_version != base_version and not Version(head_version).is_devrelease

    if not bumped:
        if label and label != "internal":
            errors.append(
                f"label {label!r} is for a pull request that raises the version; this one keeps "
                f"{base_version}, so label it 'internal'"
            )
        return errors

    if label == "internal":
        errors.append("this pull request raises the version, so its label is fix, feature, or breaking")
    errors += title_errors(title)
    entry = next((e for e in entries if e["cli_version"] == head_version), None)
    if entry is None:
        errors.append(f"add release-notes/{head_version}.json for the new version")
        return errors
    if label in CHANGE_TYPES and label != entry["change_type"]:
        errors.append(f"label {label!r} does not match change_type {entry['change_type']!r}")
    if skill_frontmatter_version and entry["skill_version"] != skill_frontmatter_version:
        errors.append(
            f"release-notes/{head_version}.json skill_version {entry['skill_version']} is not the "
            f"skill's {skill_frontmatter_version}"
        )

    section = _rollout_section(body)
    if section is None:
        errors.append(f"the PR body needs a '{ROLLOUT_HEADING}' section (see the pull request template)")
        return errors
    for field in ROLLOUT_FIELDS:
        value = section.get(field.lower(), "")
        if _PLACEHOLDER.match(value):
            errors.append(f"'{ROLLOUT_HEADING}' needs a value for '{field}'")
    expected = {
        "version": head_version,
        "change type": entry["change_type"],
        "update status": entry["update_status"],
    }
    for key, want in expected.items():
        got = section.get(key, "")
        if got and got != want:
            errors.append(f"'{ROLLOUT_HEADING}' says {key} {got!r}; release-notes/{head_version}.json says {want!r}")
    if entry["update_status"] == "urgent" and _PLACEHOLDER.match(section.get("release-owner approval", "")):
        errors.append(f"an urgent release needs 'Release-owner approval:' in '{ROLLOUT_HEADING}'")
    return errors


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=WORKSPACE, check=True, capture_output=True, text=True).stdout


def _pr_main(args: argparse.Namespace) -> list[str]:
    top = Path(_git("rev-parse", "--show-toplevel").strip())
    rel = PYPROJECT.relative_to(top).as_posix()
    base_version = package_version(_git("show", f"{args.base}:{rel}"))
    entries, errors = load_entries()
    if errors:
        return errors
    labels = [label.strip() for label in json.loads(args.labels)] if args.labels.startswith("[") else [
        label.strip() for label in args.labels.split(",") if label.strip()
    ]
    return pr_errors(
        title=args.title,
        labels=labels,
        body=Path(args.body_file).read_text(encoding="utf-8") if args.body_file else "",
        base_version=base_version,
        head_version=package_version(PYPROJECT.read_text()),
        entries=entries,
        skill_frontmatter_version=skill_version(SKILL.read_text(encoding="utf-8")),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate without writing index.json")
    sub = parser.add_subparsers(dest="command")
    pr = sub.add_parser("pr", help="check a pull request's title, label, and rollout section")
    pr.add_argument("--base", required=True, help="git ref of the base branch, for example origin/master")
    pr.add_argument("--title", required=True)
    pr.add_argument("--labels", default="", help="JSON list or comma-separated label names")
    pr.add_argument("--body-file", help="file holding the pull request body")
    args = parser.parse_args(argv)

    if args.command == "pr":
        errors = _pr_main(args)
    elif args.check:
        errors = check_errors()
    else:
        entries, errors = load_entries()
        if not errors:
            INDEX.write_text(render_index(entries), encoding="utf-8")
            print(f"wrote {INDEX.relative_to(WORKSPACE)} ({len(entries)} releases)")
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") == "true" else "error: "
    for error in errors:
        print(f"{prefix}{error}", file=sys.stderr)
    if not errors and args.command == "pr":
        print("pull request release checks passed")
    elif not errors and args.check:
        print("release notes checks passed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
