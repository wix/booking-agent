"""scripts/harness_docs.py: verified harness tables and pinned values come from the registry."""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

_WORKSPACE = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("harness_docs", _WORKSPACE / "scripts" / "harness_docs.py")
harness_docs = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("harness_docs", harness_docs)
_spec.loader.exec_module(harness_docs)

REGISTRY = {
    "schema_version": 2,
    "harnesses": {
        "grok-bot": {
            "name": "Grok Bot",
            "replay": "grok-bot",
            "status": "verified",
            "guide": "docs/harnesses/grok-bot.md",
            "replayed": {"cli": "0.9.1", "skill": "0.57.1", "date": "2026-10-02"},
            "recommended": {"cli": "0.10.0", "skill": "0.57.2"},
        },
        "acme": {
            "name": "Acme",
            "replay": "acme",
            "status": "pending",
            "guide": "docs/harnesses/acme.md",
        },
    },
}


def _workspace(tmp_path: Path) -> Path:
    guides = tmp_path / "docs" / "harnesses"
    guides.mkdir(parents=True)
    table = "<!-- verified-harnesses:start -->\nold\n<!-- verified-harnesses:end -->\n"
    (tmp_path / "README.md").write_text("# Top\n\n" + table + "\nAfter.\n")
    (guides / "README.md").write_text(table)
    pins = "# Guide\n\n<!-- harness-pins:start -->\nold\n<!-- harness-pins:end -->\n\nSteps.\n"
    (guides / "grok-bot.md").write_text(pins)
    (guides / "acme.md").write_text(pins)
    return tmp_path


def test_the_repository_docs_are_current():
    assert harness_docs.check_errors(harness_docs.release_notes.load_registry()) == []


def test_tables_link_each_guide_relative_to_the_doc():
    top = harness_docs.render_table(REGISTRY, "README.md")
    index = harness_docs.render_table(REGISTRY, "docs/harnesses/README.md")
    assert "| Grok Bot | [Install on Grok Bot](docs/harnesses/grok-bot.md) | `0.10.0` | `0.57.2` " in top
    assert "| Passed 2 Oct 2026 with CLI 0.9.1 |" in top
    assert "[Install on Grok Bot](grok-bot.md)" in index
    assert "| Acme | [Install on Acme](docs/harnesses/acme.md) | - | - | Pending |" in top
    assert top.index("Acme") < top.index("Grok Bot")


def test_pins_carry_the_approved_pair_and_the_replay():
    pins = harness_docs.render_pins(REGISTRY["harnesses"]["grok-bot"])
    assert "cli=0.10.0\nreplay=grok-bot\n```" in pins
    assert "with CLI 0.9.1 and skill 0.57.1" in pins
    assert "carries every change the replayed CLI ran" in pins
    same = copy.deepcopy(REGISTRY["harnesses"]["grok-bot"])
    same["replayed"] = {"cli": "0.10.0", "skill": "0.57.2", "date": "2026-10-02"}
    assert "carries every change" not in harness_docs.render_pins(same)
    assert "cli=" not in harness_docs.render_pins(REGISTRY["harnesses"]["acme"])


def test_check_flags_stale_docs_until_they_are_regenerated(tmp_path):
    workspace = _workspace(tmp_path)
    errors = harness_docs.check_errors(REGISTRY, workspace)
    assert sorted(errors) == sorted(
        f"{doc} is out of date: run `python scripts/harness_docs.py`"
        for doc in ("README.md", "docs/harnesses/README.md", "docs/harnesses/grok-bot.md", "docs/harnesses/acme.md")
    )
    for doc, text in harness_docs.rendered(REGISTRY, workspace).items():
        (workspace / doc).write_text(text)
    assert harness_docs.check_errors(REGISTRY, workspace) == []
    readme = (workspace / "README.md").read_text()
    assert readme.startswith("# Top\n\n<!-- verified-harnesses:start -->\n| Harness |")
    assert readme.endswith("<!-- verified-harnesses:end -->\n\nAfter.\n")


@pytest.mark.parametrize("text", ["no markers\n", "<!-- harness-pins:end -->\n<!-- harness-pins:start -->\n"])
def test_a_guide_without_one_marked_block_is_an_error(tmp_path, text):
    workspace = _workspace(tmp_path)
    (workspace / "docs" / "harnesses" / "acme.md").write_text(text)
    assert any("needs exactly one" in e for e in harness_docs.check_errors(REGISTRY, workspace))


def test_a_missing_guide_is_an_error(tmp_path):
    workspace = _workspace(tmp_path)
    (workspace / "docs" / "harnesses" / "acme.md").unlink()
    assert harness_docs.check_errors(REGISTRY, workspace) == ["docs/harnesses/acme.md does not exist"]
