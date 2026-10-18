"""The booking-agent skill this CLI was built with.

``bundled_skill.md`` next to this module is a copy of ``skills/booking-agent/SKILL.md``
(``scripts/sync_skill_bundle.py`` writes it, and CI checks it), so the installed CLI can
always print the instructions that match it, offline. It must not be named ``SKILL.md``:
skill installers treat any directory holding a ``SKILL.md`` as a skill and would install
this whole package in place of ``skills/booking-agent/``.
"""
from __future__ import annotations

import re
from pathlib import Path

SKILL_FILE = Path(__file__).with_name("bundled_skill.md")
_VERSION_LINE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def skill_text() -> str:
    return SKILL_FILE.read_text(encoding="utf-8")


def _frontmatter_version(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        match = _VERSION_LINE.search(text[4:end if end != -1 else len(text)])
        if match:
            return match.group(1)
    raise RuntimeError(f"{SKILL_FILE} has no version in its frontmatter")


SKILL_VERSION = _frontmatter_version(skill_text())
