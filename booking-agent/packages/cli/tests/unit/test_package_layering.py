"""``booking_agent`` is the protocol library under the CLI and never depends on it."""
from __future__ import annotations

import re
from pathlib import Path

import booking_agent

_CLI_IMPORT = re.compile(r"(?m)^\s*(?:from|import)\s+booking_agent_cli\b")


def test_booking_agent_never_imports_booking_agent_cli():
    root = Path(booking_agent.__file__).parent
    offenders = [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*.py"))
        if _CLI_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
