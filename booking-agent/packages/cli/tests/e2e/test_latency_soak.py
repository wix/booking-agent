"""Hermetic latency soak harness tests."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_hermetic_latency_soak_within_budget():
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "latency_soak.py"),
            "--mode",
            "hermetic",
            "--iterations",
            "3",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "discover" in proc.stdout


def test_hermetic_soak_fails_on_artificially_low_budget(tmp_path):
    budgets = tmp_path / "tight.json"
    budgets.write_text(
        '{"steps": {"discover": {"p50": 1, "p95": 1}}}',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "latency_soak.py"),
            "--mode",
            "hermetic",
            "--iterations",
            "1",
            "--budgets",
            str(budgets),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "BUDGET BREACHES" in proc.stderr
