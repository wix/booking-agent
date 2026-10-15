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
