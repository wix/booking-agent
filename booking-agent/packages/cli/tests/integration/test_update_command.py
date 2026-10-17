"""booking-agent skill / update and the update gate at flow init."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from booking_agent_cli import __version__ as CLI_VERSION
from booking_agent_cli import session as sess
from booking_agent_cli import updates
from booking_agent_cli.commands import update as update_cmd
from booking_agent_cli.skill_bundle import SKILL_FILE, SKILL_VERSION
from integration.test_flow_gates import _invoke, _stdout_json

_SKILL_SOURCE = Path(__file__).resolve().parents[4] / "skills" / "booking-agent" / "SKILL.md"


def _release(version, change="feature", status="recommended", ready=True, caps=()):
    return updates.Release(
        cli_version=version,
        skill_version="9.9.9",
        change_type=change,
        update_status=status,
        ecosystem_ready=ready,
        relevant_capabilities=tuple(caps),
        user_summary="It improves calendar connection support.",
        details_url=f"https://github.com/wix/booking-agent/releases/tag/v{version}",
    )


def _next(version):
    major, minor, patch, _ = updates.parse_version(CLI_VERSION)
    return f"{major}.{minor + version}.0"


@pytest.fixture
def checks_on(isolated_session, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_UPDATE_CHECK", "1")
    releases: list = []
    monkeypatch.setattr(updates, "load_releases", lambda **_kw: list(releases))
    return releases


@pytest.fixture
def no_shell(monkeypatch):
    calls = []

    def fake_run(argv, *, timeout):
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(update_cmd, "_run", fake_run)
    monkeypatch.setattr(update_cmd, "is_editable_install", lambda: False)
    monkeypatch.setattr(update_cmd, "detect_installer", lambda: "uv")
    return calls


# ---------------------------------------------------------------------------
# skill
# ---------------------------------------------------------------------------


def test_the_bundled_skill_matches_the_published_skill():
    assert SKILL_FILE.read_bytes() == _SKILL_SOURCE.read_bytes()


def test_skill_version_reports_the_pair(runner):
    out = _stdout_json(_invoke(runner, "skill", "version"))
    assert out["data"] == {"cli_version": CLI_VERSION, "skill_version": SKILL_VERSION}


def test_skill_print_returns_the_matching_text(runner):
    out = _stdout_json(_invoke(runner, "skill", "print"))
    assert out["data"]["skill_text"] == SKILL_FILE.read_text(encoding="utf-8")


def test_skill_print_replaces_a_loaded_skill_file(runner, tmp_path):
    target = tmp_path / "booking-agent" / "SKILL.md"
    target.parent.mkdir()
    target.write_text("---\nversion: 0.0.1\nname: booking-agent\n---\nold\n")
    r = _invoke(runner, "skill", "print", "--output", str(target))
    assert r.exit_code == 0, r.stdout
    assert target.read_bytes() == SKILL_FILE.read_bytes()


@pytest.mark.parametrize("setup", ["other_skill", "missing", "symlink", "wrong_name"])
def test_skill_print_never_overwrites_anything_else(runner, tmp_path, setup):
    target = tmp_path / ("notes.md" if setup == "wrong_name" else "SKILL.md")
    if setup == "other_skill":
        target.write_text("---\nname: something-else\n---\n")
    elif setup == "symlink":
        real = tmp_path / "checkout.md"
        real.write_text("---\nname: booking-agent\n---\n")
        target.symlink_to(real)
    elif setup == "wrong_name":
        target.write_text("---\nname: booking-agent\n---\n")
    before = target.read_text() if target.exists() else None
    r = _invoke(runner, "skill", "print", "--output", str(target))
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "skill_output_refused"
    assert (target.read_text() if target.exists() else None) == before


# ---------------------------------------------------------------------------
# update check / decline
# ---------------------------------------------------------------------------


def test_update_check_asks_only_for_an_offer_worth_asking(runner, checks_on):
    checks_on.append(_release(_next(1), status="recommended"))
    out = _stdout_json(_invoke(runner, "update", "check"))
    assert out["awaiting_user"] is None
    assert out["data"]["action"] == "inform"

    out = _stdout_json(_invoke(runner, "update", "check", "--after-booking"))
    assert out["awaiting_user"] == updates.AWAITING_UPDATE_CONSENT
    assert out["next_command"] == updates.UPDATE_COMMAND
    assert "It improves calendar connection support." in out["prompt"]


def test_a_declined_offer_is_remembered(runner, checks_on):
    checks_on.append(_release(_next(1), status="urgent"))
    out = _stdout_json(_invoke(runner, "update", "check"))
    assert out["data"]["action"] == "urgent"
    r = runner.invoke(update_cmd.update_app, ["decline", "--prompt-key", out["data"]["prompt_key"]])
    assert r.exit_code == 0, r.stdout
    assert sess.load_store().update_prompts_answered == [f"release:{_next(1)}"]
    assert _stdout_json(_invoke(runner, "update", "check"))["data"]["action"] == "none"


def test_disabled_checks_say_nothing(runner, isolated_session):
    out = _stdout_json(_invoke(runner, "update", "check", "--skill-version", "0.0.1"))
    assert out["data"]["action"] == "none"


# ---------------------------------------------------------------------------
# flow init gate
# ---------------------------------------------------------------------------


def test_flow_init_asks_about_a_skill_mismatch_before_creating_a_session(runner, checks_on):
    r = _invoke(runner, "flow", "init", "--user-request", "haircut", "--skill-version", "0.0.1")
    out = _stdout_json(r)
    assert out["awaiting_user"] == updates.AWAITING_UPDATE_CONSENT
    assert out["data"]["reason"] == "skill_older"
    assert "--skill-version 0.0.1" in out["data"]["continue_command"]
    assert sess.load_store().sessions == []

    _invoke(runner, "update", "decline", "--prompt-key", out["data"]["prompt_key"])
    out = _stdout_json(_invoke(runner, "flow", "init", "--user-request", "haircut", "--skill-version", "0.0.1"))
    assert out["awaiting_user"] != updates.AWAITING_UPDATE_CONSENT
    assert len(sess.load_store().sessions) == 1


def test_flow_init_without_the_flag_is_treated_as_a_stale_skill(runner, checks_on):
    out = _stdout_json(_invoke(runner, "flow", "init", "--user-request", "haircut"))
    assert out["data"]["reason"] == "skill_unknown"


def test_flow_init_with_a_matching_skill_does_not_ask_about_a_feature(runner, checks_on):
    checks_on.append(_release(_next(1), status="recommended"))
    out = _stdout_json(_invoke(runner, "flow", "init", "--user-request", "haircut", "--skill-version", SKILL_VERSION))
    assert out["awaiting_user"] != updates.AWAITING_UPDATE_CONSENT
    assert len(sess.load_store().sessions) == 1


def test_flow_init_asks_about_a_relevant_feature(runner, checks_on):
    checks_on.append(_release(_next(1), caps=["calendar"]))
    out = _stdout_json(_invoke(
        runner, "flow", "init", "--user-request", "haircut",
        "--skill-version", SKILL_VERSION, "--capability", "calendar",
    ))
    assert out["awaiting_user"] == updates.AWAITING_UPDATE_CONSENT
    assert out["data"]["reason"] == "relevant_release"


def test_flow_init_never_asks_mid_booking(runner, checks_on):
    checks_on.append(_release(_next(1), status="urgent"))
    _invoke(runner, "update", "decline", "--prompt-key", f"release:{_next(1)}")
    _invoke(runner, "flow", "init", "--user-request", "haircut", "--skill-version", SKILL_VERSION)
    checks_on[:] = [_release(_next(2), status="urgent")]
    out = _stdout_json(_invoke(runner, "flow", "init", "--user-request", "nails", "--skill-version", SKILL_VERSION))
    assert out["data"]["error"]["code"] == "session_active"


def test_a_failed_check_never_blocks_flow_init(runner, checks_on, monkeypatch):
    def boom(**_kw):
        raise RuntimeError("index broke")

    monkeypatch.setattr(updates, "load_releases", boom)
    out = _stdout_json(_invoke(runner, "flow", "init", "--user-request", "haircut", "--skill-version", SKILL_VERSION))
    assert out["awaiting_user"] != updates.AWAITING_UPDATE_CONSENT
    assert len(sess.load_store().sessions) == 1


# ---------------------------------------------------------------------------
# update --user-confirmed
# ---------------------------------------------------------------------------


def test_update_without_consent_changes_nothing(runner, checks_on, no_shell):
    r = _invoke(runner, "update")
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "update_not_confirmed"
    assert no_shell == []


def test_update_refuses_while_a_booking_is_in_progress(runner, checks_on, no_shell):
    checks_on.append(_release(_next(1), status="urgent"))
    _invoke(runner, "update", "decline", "--prompt-key", f"release:{_next(1)}")
    _invoke(runner, "flow", "init", "--user-request", "haircut", "--skill-version", SKILL_VERSION)
    r = _invoke(runner, "update", "--user-confirmed")
    assert _stdout_json(r)["data"]["error"]["code"] == "update_session_in_progress"
    assert no_shell == []


def test_update_leaves_an_editable_checkout_alone(runner, checks_on, no_shell, monkeypatch):
    checks_on.append(_release(_next(1), status="urgent"))
    monkeypatch.setattr(update_cmd, "is_editable_install", lambda: True)
    out = _stdout_json(_invoke(runner, "update", "--user-confirmed"))
    assert out["data"]["mode"] == "editable"
    assert out["next_command"] == update_cmd.SKILL_PRINT_COMMAND
    assert no_shell == []


def test_update_with_nothing_newer_reloads_the_matching_skill(runner, checks_on, no_shell):
    out = _stdout_json(_invoke(runner, "update", "--user-confirmed"))
    assert out["data"]["mode"] == "current"
    assert out["next_command"] == update_cmd.SKILL_PRINT_COMMAND
    assert no_shell == []


def test_update_installs_the_selected_version_not_the_newest_upload(runner, checks_on, no_shell, monkeypatch):
    checks_on.extend([
        _release(_next(1), status="recommended"),
        _release(_next(2), status="deferred", ready=False),
    ])
    monkeypatch.setattr(update_cmd, "verify_installed", lambda: {"cli_version": _next(1), "skill_version": "9.9.9"})
    out = _stdout_json(_invoke(runner, "update", "--user-confirmed"))
    assert no_shell == [["uv", "tool", "install", "--force", f"booking-agent-cli=={_next(1)}"]]
    assert out["data"]["installed_version"] == _next(1)
    assert out["next_command"] == update_cmd.SKILL_PRINT_COMMAND
    assert f"release:{_next(1)}" in sess.load_store().update_prompts_answered


def test_update_to_the_newest_release_does_not_pin_it(runner, checks_on, no_shell, monkeypatch):
    checks_on.append(_release(_next(1)))
    monkeypatch.setattr(update_cmd, "verify_installed", lambda: {"cli_version": _next(1), "skill_version": "9.9.9"})
    _invoke(runner, "update", "--user-confirmed")
    assert no_shell == [["uv", "tool", "install", "--force", f"booking-agent-cli>={_next(1)}"]]


def test_a_failed_install_is_reported_without_claiming_success(runner, checks_on, no_shell, monkeypatch):
    checks_on.append(_release(_next(1)))
    monkeypatch.setattr(update_cmd, "_run", lambda argv, *, timeout: (False, "network down"))
    r = _invoke(runner, "update", "--user-confirmed")
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "update_failed"


def test_an_install_that_does_not_start_is_reported(runner, checks_on, no_shell, monkeypatch):
    checks_on.append(_release(_next(1)))
    monkeypatch.setattr(update_cmd, "verify_installed", lambda: {"cli_version": CLI_VERSION})
    r = _invoke(runner, "update", "--user-confirmed")
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "update_not_verified"


def test_an_unknown_installer_prints_the_commands_and_changes_nothing(runner, checks_on, no_shell, monkeypatch):
    checks_on.append(_release(_next(1)))
    monkeypatch.setattr(update_cmd, "detect_installer", lambda: None)
    r = _invoke(runner, "update", "--user-confirmed")
    out = _stdout_json(r)
    assert r.exit_code == 0
    assert out["data"]["mode"] == "manual"
    assert set(out["data"]["commands"]) == {"uv", "pipx"}
    assert no_shell == []


def test_update_proceeds_when_the_saved_state_is_from_a_newer_version(runner, checks_on, no_shell, monkeypatch):
    sess.session_path().write_text(json.dumps({"schema_version": 99, "sessions": []}))
    checks_on.append(_release(_next(1)))
    monkeypatch.setattr(update_cmd, "verify_installed", lambda: {"cli_version": _next(1), "skill_version": "9.9.9"})
    out = _stdout_json(_invoke(runner, "update", "check"))
    assert (out["data"]["action"], out["data"]["reason"]) == ("urgent", "saved_state_from_newer_version")
    r = _invoke(runner, "update", "--user-confirmed")
    assert r.exit_code == 0, r.stdout
    assert len(no_shell) == 1


@pytest.mark.parametrize("prefix,installer", [
    ("/home/u/.local/share/uv/tools/booking-agent-cli", "uv"),
    ("/home/u/.local/pipx/venvs/booking-agent-cli", "pipx"),
    ("/home/u/project/.venv", None),
])
def test_detect_installer(monkeypatch, prefix, installer):
    monkeypatch.setattr(sys, "prefix", prefix)
    monkeypatch.setattr(update_cmd.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert update_cmd.detect_installer() == installer


def test_the_skill_passes_its_own_version_on_every_command():
    import re

    text = _SKILL_SOURCE.read_text(encoding="utf-8")
    passed = set(re.findall(r"--skill-version\s+(\S+?)[`\s)]", text))
    assert passed == {SKILL_VERSION}
