"""Which update, if any, the user is told about."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from booking_agent_cli import updates
from booking_agent_cli.skill_bundle import SKILL_VERSION

_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "release_notes.py"
_spec = importlib.util.spec_from_file_location("release_notes", _SCRIPT)
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)


def _entry(version, change="feature", status="recommended", ready=True, caps=(), **extra):
    return {
        "schema_version": 1,
        "cli_version": version,
        "skill_version": SKILL_VERSION,
        "change_type": change,
        "update_status": status,
        "ecosystem_ready": ready,
        "relevant_capabilities": list(caps),
        "user_summary": f"Release {version}.",
        "details_url": f"https://github.com/wix/booking-agent/releases/tag/v{version}",
        **extra,
    }


def _releases(*entries):
    return updates.parse_index({"schema_version": 1, "releases": list(entries)})


def _decide(*entries, installed="0.1.0", **kw):
    kw.setdefault("reported_skill", SKILL_VERSION)
    return updates.decide(releases=_releases(*entries), installed=installed, **kw)


def test_enums_match_the_release_policy_script():
    assert updates.CHANGE_TYPES == release_notes.CHANGE_TYPES
    assert updates.UPDATE_STATUSES == release_notes.UPDATE_STATUSES
    assert updates.CAPABILITIES == release_notes.CAPABILITIES


def test_the_committed_index_parses():
    index = Path(__file__).resolve().parents[4] / "release-notes" / "index.json"
    releases = updates.parse_index(json.loads(index.read_text()))
    assert releases and all(updates.parse_version(r.cli_version) for r in releases)


@pytest.mark.parametrize("text,ok", [
    ("0.2.0", True), ("1.10.3", True), ("0.2.0.dev1", True),
    ("v0.2.0", False), ("0.2", False), ("latest", False), (None, False), (3, False),
])
def test_parse_version(text, ok):
    assert (updates.parse_version(text) is not None) is ok


def test_a_dev_build_sorts_before_its_release():
    assert updates.parse_version("0.2.0.dev3") < updates.parse_version("0.2.0")


@pytest.mark.parametrize("status,change,action", [
    ("available", "fix", "none"),
    ("available", "feature", "none"),
    ("recommended", "fix", "inform"),
    ("recommended", "feature", "inform"),
    ("recommended", "breaking", "recommend"),
    ("urgent", "fix", "urgent"),
    ("deferred", "feature", "none"),
    ("deferred", "breaking", "none"),
])
def test_status_maps_to_one_action(status, change, action):
    ready = status != "deferred"
    assert _decide(_entry("0.2.0", change, status, ready)).action == action


def test_a_recommended_feature_is_offered_after_the_booking():
    assert _decide(_entry("0.2.0"), after_booking=True).action == "recommend"


def test_a_recommended_feature_is_offered_when_the_request_needs_it():
    d = _decide(_entry("0.2.0", caps=["calendar"]), capabilities=["calendar"])
    assert (d.action, d.reason) == ("recommend", "relevant_release")
    assert _decide(_entry("0.2.0", caps=["calendar"]), capabilities=["payment"]).action == "inform"


def test_not_ready_is_treated_as_deferred_even_if_marked_recommended():
    assert _decide(_entry("0.2.0", status="urgent", ready=False)).action == "none"


def test_a_deferred_release_holds_back_every_later_one():
    d = _decide(
        _entry("0.2.0", status="deferred", ready=False),
        _entry("0.3.0", status="urgent"),
    )
    assert d.action == "none" and d.target_version is None


def test_the_target_is_the_newest_safe_release_and_carries_the_strongest_status():
    d = _decide(
        _entry("0.1.1", "fix", "urgent"),
        _entry("0.2.0", "feature", "available"),
        _entry("0.3.0", "breaking", "deferred", ready=False),
    )
    assert (d.action, d.target_version, d.update_status) == ("urgent", "0.2.0", "urgent")
    assert d.user_summary == "Release 0.1.1."
    assert d.change_type == "feature"


def test_a_withdrawn_release_is_never_the_target():
    d = _decide(
        _entry("0.2.0", status="urgent"),
        _entry("0.3.0", status="withdrawn", replacement_version="0.3.1"),
    )
    assert d.target_version == "0.2.0"


def test_a_withdrawn_installed_version_is_urgent_once_its_replacement_exists():
    d = _decide(
        _entry("0.2.0", status="withdrawn", replacement_version="0.2.1"),
        _entry("0.2.1", "fix", "available"),
        installed="0.2.0",
    )
    assert (d.action, d.reason, d.target_version) == ("urgent", "installed_version_withdrawn", "0.2.1")


def test_nothing_newer_means_nothing_to_say():
    assert _decide(_entry("0.1.0")).action == "none"


def test_no_index_means_no_recommendation():
    d = updates.decide(reported_skill=SKILL_VERSION, releases=None)
    assert d.action == "none"


def test_an_answered_offer_is_not_made_again():
    d = _decide(_entry("0.2.0", status="urgent"), answered=["release:0.2.0"])
    assert (d.action, d.reason) == ("none", "already_answered")


def test_malformed_entries_are_skipped():
    bad = [
        {**_entry("0.2.0"), "change_type": "major"},
        {**_entry("0.3.0"), "update_status": "now"},
        {**_entry("0.4.0"), "cli_version": "latest"},
        {**_entry("0.5.0"), "schema_version": 2},
        "not an object",
    ]
    assert updates.parse_index({"schema_version": 1, "releases": bad}) == []
    assert updates.parse_index({"schema_version": 2, "releases": [_entry("0.2.0")]}) == []
    assert updates.parse_index(None) == []


def test_only_project_release_links_are_passed_on():
    (r,) = _releases({**_entry("0.2.0"), "details_url": "https://evil.example/x"})
    assert r.details_url == ""


@pytest.mark.parametrize("reported,reason", [
    (None, "skill_unknown"),
    ("", "skill_unknown"),
    ("banana", "skill_unknown"),
    ("0.0.1", "skill_older"),
    ("999.0.0", "skill_newer"),
])
def test_a_skill_mismatch_is_urgent_without_network(reported, reason):
    d = updates.decide(reported_skill=reported, releases=None)
    assert (d.action, d.reason) == ("urgent", reason)
    assert d.prompt_key


def test_a_matching_skill_is_not_a_reason_to_ask():
    assert updates.local_pair_reason(SKILL_VERSION) is None


def test_a_declined_skill_mismatch_is_not_asked_again():
    first = updates.decide(reported_skill="0.0.1", releases=None)
    again = updates.decide(reported_skill="0.0.1", releases=None, answered=[first.prompt_key])
    assert again.action == "none"


def test_a_newer_saved_state_is_urgent():
    d = updates.decide(reported_skill=SKILL_VERSION, releases=None, store_too_new=True)
    assert (d.action, d.reason) == ("urgent", "saved_state_from_newer_version")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


@pytest.fixture
def checks_on(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_UPDATE_CHECK", "1")
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("BOOKING_AGENT_RELEASE_INDEX_URL", "https://example.test/index.json")
    return tmp_path


def test_disabled_checks_never_fetch(monkeypatch):
    monkeypatch.setattr(updates, "_http_get", lambda url: pytest.fail("fetched"))
    assert updates.load_releases() is None


def test_a_failed_fetch_means_no_recommendation(checks_on, monkeypatch):
    def boom(url):
        raise OSError("offline")

    monkeypatch.setattr(updates, "_http_get", boom)
    assert updates.load_releases() is None


def test_the_index_is_cached_for_a_day(checks_on, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return {"schema_version": 1, "releases": [_entry("0.2.0")]}

    monkeypatch.setattr(updates, "_http_get", fetch)
    assert [r.cli_version for r in updates.load_releases(now=1000.0)] == ["0.2.0"]
    assert [r.cli_version for r in updates.load_releases(now=1000.0 + 3600)] == ["0.2.0"]
    assert len(calls) == 1
    updates.load_releases(now=1000.0 + updates.CACHE_TTL_SECONDS + 1)
    assert len(calls) == 2


def test_a_stale_cache_is_used_when_the_fetch_fails(checks_on, monkeypatch):
    monkeypatch.setattr(updates, "_http_get", lambda url: {"schema_version": 1, "releases": [_entry("0.2.0")]})
    updates.load_releases(now=0.0)

    def boom(url):
        raise OSError("offline")

    monkeypatch.setattr(updates, "_http_get", boom)
    assert [r.cli_version for r in updates.load_releases(now=10.0 * updates.CACHE_TTL_SECONDS)] == ["0.2.0"]


def test_a_non_https_index_url_is_never_fetched(checks_on, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_RELEASE_INDEX_URL", "http://example.test/index.json")
    monkeypatch.setattr(updates, "_http_get", lambda url: pytest.fail("fetched"))
    assert updates.load_releases() is None
