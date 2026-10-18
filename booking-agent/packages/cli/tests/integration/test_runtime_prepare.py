"""`booking-agent runtime prepare` links runtime state into a folder that survives restarts."""
import json
import os
import stat

import pytest

from booking_agent_cli import runtime_prepare
from booking_agent_cli.main import app


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "keep"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(home / ".booking-agent"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    link_dir = home / "link-config"
    monkeypatch.setattr(runtime_prepare, "link_cli_config_dir", lambda: link_dir)
    return home, root, link_dir


def _run(runner, *args):
    result = runner.invoke(app, ["runtime", "prepare", *args])
    return result, json.loads(result.stdout)


def test_links_state_and_link_sign_in_into_the_root(runner, dirs):
    home, root, link_dir = dirs
    result, body = _run(runner, "--persistent-root", str(root))

    assert result.exit_code == 0, result.stdout
    state = home / ".booking-agent"
    assert state.is_symlink() and state.resolve() == (root / "booking-agent").resolve()
    assert link_dir.is_symlink() and link_dir.resolve() == (root / "link-cli").resolve()
    assert stat.S_IMODE((root / "booking-agent").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "link-cli").stat().st_mode) == 0o700
    assert body["next_command"] == "booking-agent flow sessions list"
    assert [link["result"] for link in body["data"]["links"]] == ["linked", "linked"]


def test_is_idempotent(runner, dirs):
    _home, root, _link_dir = dirs
    _run(runner, "--persistent-root", str(root))
    result, body = _run(runner, "--persistent-root", str(root))

    assert result.exit_code == 0
    assert [link["result"] for link in body["data"]["links"]] == ["already_linked", "already_linked"]


def test_moves_existing_contents_and_keeps_their_modes(runner, dirs):
    _home, root, link_dir = dirs
    link_dir.mkdir(parents=True)
    secret = link_dir / "config.json"
    secret.write_text("{}")
    os.chmod(secret, 0o600)

    result, _body = _run(runner, "--persistent-root", str(root))

    assert result.exit_code == 0, result.stdout
    moved = root / "link-cli" / "config.json"
    assert moved.read_text() == "{}"
    assert stat.S_IMODE(moved.stat().st_mode) == 0o600
    assert (link_dir / "config.json").read_text() == "{}"


def test_keeps_the_runtime_copy_aside_when_both_hold_data(runner, dirs):
    _home, root, link_dir = dirs
    (root / "link-cli").mkdir(parents=True)
    (root / "link-cli" / "config.json").write_text("persistent")
    link_dir.mkdir(parents=True)
    (link_dir / "config.json").write_text("runtime")

    result, body = _run(runner, "--persistent-root", str(root))

    assert result.exit_code == 0, result.stdout
    assert (link_dir / "config.json").read_text() == "persistent"
    aside = body["data"]["links"][1]["kept_runtime_copy_at"]
    assert open(os.path.join(aside, "config.json")).read() == "runtime"


def test_extra_links_resolve_relative_paths_under_the_root(runner, dirs, tmp_path):
    _home, root, _link_dir = dirs
    cache = tmp_path / "tool-cache"
    result, body = _run(runner, "--persistent-root", str(root), "--link", f"{cache}=caches/tools")

    assert result.exit_code == 0, result.stdout
    assert cache.is_symlink() and cache.resolve() == (root / "caches" / "tools").resolve()
    assert body["data"]["links"][2]["result"] == "linked"


@pytest.mark.parametrize("args", [
    ["--persistent-root", "relative/root"],
    ["--link", "no-equals-sign"],
    ["--link", "relative/runtime=x"],
])
def test_rejects_ambiguous_paths(runner, dirs, args):
    _home, root, _link_dir = dirs
    if args[0] == "--link":
        args = ["--persistent-root", str(root), *args]
    result, body = _run(runner, *args)

    assert result.exit_code == 1
    assert body["data"]["error"]["code"] == "runtime_prepare_failed"


def test_never_runs_link_login(runner, dirs, monkeypatch):
    import subprocess

    _home, root, _link_dir = dirs

    def _no_subprocess(*_a, **_k):
        raise AssertionError("runtime prepare must not run other programs")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)
    result, _body = _run(runner, "--persistent-root", str(root))
    assert result.exit_code == 0, result.stdout


# --- A changed or nested root must never split the saved state (reset regression) ---------------

def _seed(root_state, root_link, *, session="session", link="link"):
    root_state.mkdir(parents=True, exist_ok=True)
    root_link.mkdir(parents=True, exist_ok=True)
    (root_state / "booking-agent-session.json").write_text(session)
    (root_link / "config.json").write_text(link)


def test_a_root_already_named_booking_agent_is_not_nested_again(runner, dirs):
    home, root, link_dir = dirs
    nested = root / "booking-agent"

    result, body = _run(runner, "--persistent-root", str(nested))

    assert result.exit_code == 0, result.stdout
    assert (home / ".booking-agent").resolve() == (root / "booking-agent").resolve()
    assert link_dir.resolve() == (root / "link-cli").resolve()
    assert not (root / "booking-agent" / "booking-agent").exists()
    assert not (root / "booking-agent" / "link-cli").exists()
    assert body["data"]["root"] == str(root)


def test_both_spellings_of_the_same_root_share_one_state(runner, dirs):
    home, root, link_dir = dirs
    _run(runner, "--persistent-root", str(root))
    (root / "booking-agent" / "booking-agent-session.json").write_text("real")
    (root / "link-cli" / "config.json").write_text("signed-in")

    result, _body = _run(runner, "--persistent-root", str(root / "booking-agent"))

    assert result.exit_code == 0, result.stdout
    assert (home / ".booking-agent" / "booking-agent-session.json").read_text() == "real"
    assert (link_dir / "config.json").read_text() == "signed-in"


def test_a_state_folder_nested_by_the_old_layout_is_lifted_back_up(runner, dirs):
    home, root, link_dir = dirs
    old = root / "booking-agent"
    _seed(old / "booking-agent", old / "link-cli", session="real", link="signed-in")

    result, body = _run(runner, "--persistent-root", str(old))

    assert result.exit_code == 0, result.stdout
    assert (home / ".booking-agent" / "booking-agent-session.json").read_text() == "real"
    assert (link_dir / "config.json").read_text() == "signed-in"
    assert not (root / "booking-agent" / "booking-agent").exists()
    assert not (root / "booking-agent" / "link-cli").exists()
    assert any(link.get("lifted_from_nested_layout") for link in body["data"]["links"])


def test_moving_to_a_different_root_brings_the_saved_state_along(runner, dirs, tmp_path):
    home, root, link_dir = dirs
    _run(runner, "--persistent-root", str(root))
    (root / "booking-agent" / "booking-agent-session.json").write_text("real")
    (root / "link-cli" / "config.json").write_text("signed-in")
    other = tmp_path / "other-keep"

    result, body = _run(runner, "--persistent-root", str(other))

    assert result.exit_code == 0, result.stdout
    assert (home / ".booking-agent").resolve() == (other / "booking-agent").resolve()
    assert (home / ".booking-agent" / "booking-agent-session.json").read_text() == "real"
    assert (link_dir / "config.json").read_text() == "signed-in"
    assert [link["result"] for link in body["data"]["links"]][:2] == ["relinked", "relinked"]


def test_moving_to_a_root_that_has_its_own_state_leaves_the_old_one_untouched(runner, dirs, tmp_path):
    home, root, link_dir = dirs
    _run(runner, "--persistent-root", str(root))
    (root / "link-cli" / "config.json").write_text("old-sign-in")
    other = tmp_path / "other-keep"
    _seed(other / "booking-agent", other / "link-cli", session="newer", link="new-sign-in")

    result, body = _run(runner, "--persistent-root", str(other))

    assert result.exit_code == 0, result.stdout
    assert (link_dir / "config.json").read_text() == "new-sign-in"
    assert (root / "link-cli" / "config.json").read_text() == "old-sign-in"
    assert body["data"]["links"][1]["previous_root_left_at"] == str((root / "link-cli").resolve())


def test_doctor_reports_where_state_lives_and_what_is_orphaned(runner, dirs, tmp_path):
    home, root, link_dir = dirs
    _run(runner, "--persistent-root", str(root))
    _seed(root / "booking-agent" / "booking-agent", root / "booking-agent" / "link-cli")

    result = runner.invoke(app, ["runtime", "doctor"])
    body = json.loads(result.stdout)

    assert result.exit_code == 0, result.stdout
    assert body["data"]["state"]["linked_to"] == str((root / "booking-agent").resolve())
    assert body["data"]["link_sign_in"]["linked_to"] == str((root / "link-cli").resolve())
    kinds = {finding["kind"] for finding in body["data"]["findings"]}
    assert "nested_state_folder" in kinds
    assert body["data"]["healthy"] is False


def test_doctor_is_quiet_when_everything_is_in_one_place(runner, dirs):
    _home, root, _link_dir = dirs
    _run(runner, "--persistent-root", str(root))

    result = runner.invoke(app, ["runtime", "doctor"])
    body = json.loads(result.stdout)

    assert result.exit_code == 0, result.stdout
    assert body["data"]["healthy"] is True
    assert body["data"]["findings"] == []
