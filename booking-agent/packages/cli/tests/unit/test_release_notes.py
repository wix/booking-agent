"""scripts/release_notes.py: release metadata rules and the pull request release check."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

_WORKSPACE = Path(__file__).resolve().parents[4]
_spec = importlib.util.spec_from_file_location("release_notes", _WORKSPACE / "scripts" / "release_notes.py")
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

TITLE = "Tell users when a newer booking assistant is ready"
BODY = """\
Summary.

## Release and rollout

- Version: 0.2.0
- Change type: feature
- Update status: available
- Affected capabilities: booking
- Interoperability impact: none; no USP, UCP, registry, or business request changes
- Readiness evidence: unchanged wire contracts; full suite passes
- Release-owner approval: n/a
"""


def _entry(version="0.2.0", **overrides):
    entry = {
        "schema_version": 1,
        "cli_version": version,
        "skill_version": "0.48.0",
        "change_type": "feature",
        "update_status": "available",
        "ecosystem_ready": True,
        "relevant_capabilities": ["booking"],
        "user_summary": "Lets the assistant tell you when an update is ready.",
        "details_url": f"https://github.com/wix/booking-agent/releases/tag/v{version}",
        "readiness_evidence": ["No USP, UCP, registry, or business request changed."],
    }
    entry.update(overrides)
    return entry


def _pr(**overrides):
    kwargs = {
        "title": TITLE,
        "labels": ["feature"],
        "body": BODY,
        "base_version": "0.1.0",
        "head_version": "0.2.0",
        "entries": [_entry("0.1.0", skill_version="0.46.0"), _entry()],
        "skill_frontmatter_version": "0.48.0",
    }
    kwargs.update(overrides)
    return release_notes.pr_errors(**kwargs)


def test_the_repository_release_notes_pass():
    assert release_notes.check_errors() == []


def test_a_valid_entry_has_no_errors():
    assert release_notes.entry_errors(_entry()) == []


@pytest.mark.parametrize("overrides, fragment", [
    ({"update_status": "recommended", "ecosystem_ready": False}, "ecosystem_ready false"),
    ({"update_status": "available", "ecosystem_ready": False}, "ecosystem_ready false"),
    ({"readiness_evidence": []}, "needs readiness_evidence"),
    ({"update_status": "urgent", "readiness_evidence": [" "]}, "non-empty strings"),
    ({"update_status": "withdrawn"}, "needs replacement_version"),
    ({"replacement_version": "0.2.1"}, "only for withdrawn"),
    ({"change_type": "chore"}, "change_type must be one of"),
    ({"update_status": "maybe"}, "update_status must be one of"),
    ({"relevant_capabilities": ["teleport"]}, "relevant_capabilities"),
    ({"user_summary": "Run `booking-agent update` now."}, "must not name"),
    ({"user_summary": "Adds a thing. Adds another."}, "one plain sentence"),
    ({"user_summary": "No full stop"}, "one plain sentence"),
    ({"details_url": "https://example.com"}, "details_url must be"),
    ({"surprise": 1}, "unknown keys"),
])
def test_entry_rules(overrides, fragment):
    errors = release_notes.entry_errors(_entry(**overrides))
    assert any(fragment in e for e in errors), errors


def test_a_verified_replay_rollout_is_checked_on_its_own():
    ready = {
        "update_status": "recommended",
        "ecosystem_ready": True,
        "readiness_evidence": ["Hosted replay (grok-bot): booked, no calendar event."],
    }
    entry = _entry(
        version="0.5.0", change_type="fix", relevant_capabilities=["payment"],
        readiness_evidence=["Hosted replay: general run."],
        replay_rollouts={"grok-bot": ready},
    )
    assert release_notes.entry_errors(entry) == []
    missing = copy.deepcopy(entry)
    missing["replay_rollouts"]["grok-bot"]["readiness_evidence"] = ["Offline tests passed."]
    assert any("Hosted replay (grok-bot):" in e for e in release_notes.entry_errors(missing))
    unknown = copy.deepcopy(entry)
    unknown["replay_rollouts"] = {"cursor": ready}
    assert any("not a verified replay" in e for e in release_notes.entry_errors(unknown))


def _replayed(version, skill, status="recommended"):
    return _entry(
        version,
        change_type="fix",
        skill_version=skill,
        update_status="deferred",
        replay_rollouts={
            "grok-bot": {
                "update_status": status,
                "ecosystem_ready": status != "deferred",
                "readiness_evidence": ["Hosted replay (grok-bot): booked."],
            }
        },
    )


def _registry(tmp_path, **overrides):
    (tmp_path / "docs" / "harnesses").mkdir(parents=True)
    (tmp_path / "docs" / "harnesses" / "grok-bot.md").write_text("guide\n")
    harness = {
        "name": "Grok Bot",
        "replay": "grok-bot",
        "status": "verified",
        "guide": "docs/harnesses/grok-bot.md",
        "replayed": {"cli": "0.2.0", "skill": "0.48.0", "date": "2026-10-22"},
        "recommended": {"cli": "0.2.1", "skill": "0.48.1"},
    }
    harness.update(overrides)
    return {"schema_version": 2, "harnesses": {"grok-bot": harness}}


REGISTRY_ENTRIES = [
    _replayed("0.2.0", "0.48.0"),
    _replayed("0.2.1", "0.48.1"),
    _replayed("0.2.2", "0.48.2", status="deferred"),
]


def test_the_registry_pins_the_newest_pair_its_replay_recommends(tmp_path):
    assert release_notes.registry_errors(_registry(tmp_path), REGISTRY_ENTRIES, tmp_path) == []


@pytest.mark.parametrize("overrides, fragment", [
    ({"recommended": {"cli": "0.2.0", "skill": "0.48.0"}}, "is not 0.2.1, the newest release"),
    ({"recommended": {"cli": "0.2.2", "skill": "0.48.2"}}, "is not 0.2.1, the newest release"),
    ({"recommended": {"cli": "0.2.1", "skill": "0.48.0"}}, "the skill of release 0.2.1"),
    ({"replayed": {"cli": "0.1.9", "skill": "0.48.0", "date": "2026-10-22"}}, "has no release notes"),
    ({"replayed": {"cli": "0.2.0", "skill": "0.47.0", "date": "2026-10-22"}}, "the skill of release 0.2.0"),
    ({"replayed": {"cli": "0.2.0", "skill": "0.48.0", "date": "22/10/26"}}, "YYYY-MM-DD"),
    ({"guide": "docs/harnesses/missing.md"}, "does not exist"),
    ({"guide": "README.md"}, "docs/harnesses/<name>.md"),
    ({"status": "pending"}, "unknown keys"),
    ({"status": "maybe"}, "status must be one of"),
    ({"name": " "}, "name must be"),
])
def test_registry_rules(tmp_path, overrides, fragment):
    errors = release_notes.registry_errors(_registry(tmp_path, **overrides), REGISTRY_ENTRIES, tmp_path)
    assert any(fragment in e for e in errors), errors


def test_a_pending_harness_has_no_recommended_replay(tmp_path):
    registry = _registry(tmp_path)
    pending = registry["harnesses"]["grok-bot"]
    del pending["replayed"], pending["recommended"]
    pending["status"] = "pending"
    errors = release_notes.registry_errors(registry, REGISTRY_ENTRIES, tmp_path)
    assert any("is pending, but release 0.2.1 recommends" in e for e in errors), errors
    assert release_notes.registry_errors(registry, [_replayed("0.2.2", "0.48.2", "deferred")], tmp_path) == []


def test_the_registry_schema_version_is_checked():
    assert release_notes.registry_shape_errors({"schema_version": 1, "harnesses": {"grok-bot": "grok-bot"}})


def test_a_deferred_release_may_be_not_ready():
    assert release_notes.entry_errors(
        _entry(update_status="deferred", ecosystem_ready=False, readiness_evidence=[])
    ) == []


@pytest.mark.parametrize("before, after, kind, ok", [
    ("0.1.0", "0.1.1", "fix", True),
    ("0.1.0", "0.1.1", "feature", False),
    ("0.1.0", "0.2.0", "feature", True),
    ("0.1.0", "0.2.0", "breaking", True),
    ("0.1.0", "0.2.0", "fix", False),
    ("1.0.0", "1.1.0", "breaking", False),
    ("1.0.0", "1.1.0", "feature", True),
    ("1.2.0", "2.0.0", "breaking", True),
    ("1.2.0", "2.0.0", "feature", False),
    ("0.9.0", "1.0.0", "breaking", True),
])
def test_change_type_follows_the_semver_step(before, after, kind, ok):
    errors = release_notes.series_errors([_entry(before), _entry(after, change_type=kind)])
    assert (errors == []) is ok, errors


def test_withdrawn_needs_an_installable_newer_replacement():
    withdrawn = _entry("0.2.0", update_status="withdrawn", replacement_version="0.2.1")
    assert release_notes.series_errors([withdrawn, _entry("0.2.1", change_type="fix")]) == []
    deferred = _entry("0.2.1", change_type="fix", update_status="deferred")
    assert "must be installable" in " ".join(release_notes.series_errors([withdrawn, deferred]))
    assert "has no entry" in " ".join(release_notes.series_errors([withdrawn]))


def test_check_requires_an_entry_for_the_current_version(tmp_path):
    notes = tmp_path / "release-notes"
    notes.mkdir()
    (notes / "0.1.0.json").write_text(json.dumps(_entry("0.1.0")))
    (notes / "index.json").write_text(release_notes.render_index([_entry("0.1.0")]))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "x"\nversion = "0.2.0"\n')
    assert any("0.2.0.json is missing" in e for e in release_notes.check_errors(notes, pyproject))
    pyproject.write_text('[project]\nname = "x"\nversion = "0.1.0"\n')
    assert release_notes.check_errors(notes, pyproject) == []
    (notes / "index.json").write_text("{}\n")
    assert any("out of date" in e for e in release_notes.check_errors(notes, pyproject))


def test_a_ready_bump_passes():
    assert _pr() == []


@pytest.mark.parametrize("labels, fragment", [
    ([], "exactly one release label"),
    (["feature", "fix"], "exactly one release label"),
    (["internal"], "label is fix, feature, or breaking"),
    (["fix"], "does not match change_type"),
])
def test_bump_labels(labels, fragment):
    assert any(fragment in e for e in _pr(labels=labels))


@pytest.mark.parametrize("title", [
    "Bump booking-agent-cli to 0.2.0",
    "feat: add update prompts",
    "Update flow.py for updates",
    "Sync booking-agent/ from private source",
    "Add update prompts (#44)",
    "add update prompts to the assistant",
    "Fix",
])
def test_mechanical_titles_are_rejected(title):
    assert any("becomes the GitHub Release note" in e for e in _pr(title=title))


def test_a_bump_needs_its_metadata_file_and_matching_skill():
    assert any("add release-notes/0.2.0.json" in e for e in _pr(entries=[_entry("0.1.0")]))
    assert any("is not the skill's" in e for e in _pr(skill_frontmatter_version="0.49.0"))


def test_a_bump_needs_a_filled_rollout_section():
    assert any("needs a '## Release and rollout' section" in e for e in _pr(body="Summary only."))
    blank = BODY.replace("unchanged wire contracts; full suite passes", "TBD")
    assert any("'Readiness evidence'" in e for e in _pr(body=blank))
    wrong = BODY.replace("Update status: available", "Update status: recommended")
    assert any("says update status 'recommended'" in e for e in _pr(body=wrong))


def test_urgent_needs_release_owner_approval():
    entries = [_entry("0.1.0"), _entry(update_status="urgent")]
    body = BODY.replace("Update status: available", "Update status: urgent")
    assert any("Release-owner approval" in e for e in _pr(entries=entries, body=body))
    approved = body.replace("Release-owner approval: n/a", "Release-owner approval: @release-owner, payment safety fix")
    assert _pr(entries=entries, body=approved) == []


def test_a_pull_request_without_a_bump_is_internal():
    same = {"head_version": "0.1.0"}
    assert _pr(labels=["internal"], title="docs", body="", **same) == []
    assert any("label it 'internal'" in e for e in _pr(labels=["feature"], **same))
    assert any("exactly one release label" in e for e in _pr(labels=["documentation"], **same))


def test_entries_are_never_mutated_by_the_check():
    entries = [_entry("0.1.0"), _entry()]
    before = copy.deepcopy(entries)
    _pr(entries=entries)
    assert entries == before


def test_a_ready_sign_in_calendar_or_payment_change_needs_a_hosted_replay_line():
    entry = _entry(version="0.5.0", relevant_capabilities=["calendar"])
    errors = release_notes.entry_errors(entry, "0.5.0.json")
    assert any("Hosted replay:" in e for e in errors)

    entry["readiness_evidence"] = ["Hosted replay: restart mid-booking, one reconnect message, booked + calendar link."]
    assert release_notes.entry_errors(entry, "0.5.0.json") == []


def test_the_hosted_replay_rule_leaves_other_changes_and_older_releases_alone():
    assert release_notes.entry_errors(_entry(version="0.5.0", relevant_capabilities=["search"]), "0.5.0.json") == []
    assert release_notes.entry_errors(_entry(version="0.4.2", relevant_capabilities=["payment"]), "0.4.2.json") == []
    deferred = _entry(version="0.5.0", relevant_capabilities=["payment"], ecosystem_ready=False, update_status="deferred")
    assert release_notes.entry_errors(deferred, "0.5.0.json") == []
