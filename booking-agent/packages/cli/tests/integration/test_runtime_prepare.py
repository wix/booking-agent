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


def test_merges_when_both_hold_data_and_keeps_the_older_file_aside(runner, dirs):
    _home, root, link_dir = dirs
    (root / "link-cli").mkdir(parents=True)
    (root / "link-cli" / "config.json").write_text("older")
    link_dir.mkdir(parents=True)
    (link_dir / "config.json").write_text("newer")
    (link_dir / "extra.json").write_text("only-here")
    os.utime(root / "link-cli" / "config.json", (1_000, 1_000))
    os.utime(link_dir / "config.json", (2_000, 2_000))

    result, body = _run(runner, "--persistent-root", str(root))

    assert result.exit_code == 0, result.stdout
    assert (link_dir / "config.json").read_text() == "newer"
    assert (link_dir / "extra.json").read_text() == "only-here"
    aside = body["data"]["links"][1]["older_copies_kept_at"]
    assert open(os.path.join(aside, "config.json")).read() == "older"


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


def test_moving_to_a_root_that_has_its_own_state_merges_both(runner, dirs, tmp_path):
    home, root, link_dir = dirs
    _run(runner, "--persistent-root", str(root))
    (root / "link-cli" / "config.json").write_text("old-sign-in")
    (root / "link-cli" / "only-old.json").write_text("kept")
    os.utime(root / "link-cli" / "config.json", (1_000, 1_000))
    other = tmp_path / "other-keep"
    _seed(other / "booking-agent", other / "link-cli", session="newer", link="new-sign-in")
    os.utime(other / "link-cli" / "config.json", (2_000, 2_000))

    result, body = _run(runner, "--persistent-root", str(other))

    assert result.exit_code == 0, result.stdout
    assert (link_dir / "config.json").read_text() == "new-sign-in"
    assert (link_dir / "only-old.json").read_text() == "kept"
    assert body["data"]["links"][1]["merged_existing_contents"] == "true"
    assert not (root / "link-cli").exists()


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


# --- Two booking files from different times are merged, never one hiding the other ---------------

def _store(*sessions, active=None, created=None):
    return {
        "schema_version": 2, "sessions_created": created or len(sessions),
        "active_session_id": active, "sessions": list(sessions),
    }


def test_the_further_along_copy_of_a_session_wins_and_no_booking_is_lost():
    stale = {"id": "s1", "flow_phase": "INIT"}
    real = {"id": "s1", "flow_phase": "CONFIRMED", "booking": {"id": "b1"}}
    other = {"id": "s2", "flow_phase": "PREREQ_OK"}
    merged = runtime_prepare.merge_session_stores(
        _store(stale, active="s1"), _store(real, other, active="s2"),
    )
    by_id = {s["id"]: s for s in merged["sessions"]}
    assert by_id["s1"]["flow_phase"] == "CONFIRMED" and by_id["s1"]["booking"] == {"id": "b1"}
    assert set(by_id) == {"s1", "s2"}
    assert merged["active_session_id"] == "s1"


def test_the_recorded_hosted_layout_is_repaired_without_losing_the_real_booking(runner, dirs):
    """A stale INIT store at the top level, the real one nested, a fresh store from today in /root."""
    home, root, link_dir = dirs
    state = root / "booking-agent"
    state.mkdir(parents=True)
    (state / ".usp-session.json").write_text(json.dumps(_store({"id": "run1", "flow_phase": "INIT"})))
    nested = state / "booking-agent"
    nested.mkdir()
    (nested / ".usp-session.json").write_text(json.dumps(_store(
        {"id": "run1", "flow_phase": "PAYMENT_PENDING"}, {"id": "run1b", "flow_phase": "CONFIRMED"},
    )))
    (state / "link-cli").mkdir()
    (state / "link-cli" / "config.json").write_text("nested-link")
    live = home / ".booking-agent"
    live.mkdir()
    (live / ".usp-session.json").write_text(json.dumps(_store({"id": "today", "flow_phase": "CONFIRMED"})))

    result, _body = _run(runner, "--persistent-root", str(root))

    assert result.exit_code == 0, result.stdout
    merged = json.loads((home / ".booking-agent" / ".usp-session.json").read_text())
    phases = {s["id"]: s["flow_phase"] for s in merged["sessions"]}
    assert phases == {"run1": "PAYMENT_PENDING", "run1b": "CONFIRMED", "today": "CONFIRMED"}
    assert (link_dir / "config.json").read_text() == "nested-link"
    assert not (state / "booking-agent").exists() and not (state / "link-cli").exists()


# --- A host that names its persistent folder needs no instruction from the assistant ------------

def test_the_named_persistent_folder_is_prepared_before_any_command(runner, dirs, monkeypatch):
    home, root, link_dir = dirs
    monkeypatch.setenv("BOOKING_AGENT_PERSISTENT_ROOT", str(root))

    result = runner.invoke(app, ["flow", "sessions", "list"])

    assert result.exit_code == 0, result.stdout
    assert (home / ".booking-agent").is_symlink()
    assert (home / ".booking-agent").resolve() == (root / "booking-agent").resolve()
    assert link_dir.resolve() == (root / "link-cli").resolve()


def test_without_the_variable_nothing_is_linked(runner, dirs):
    home, _root, _link_dir = dirs
    runner.invoke(app, ["flow", "sessions", "list"])
    assert not (home / ".booking-agent").is_symlink()


def test_a_bad_persistent_folder_never_blocks_a_command(runner, dirs, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_PERSISTENT_ROOT", "relative/path")
    result = runner.invoke(app, ["flow", "sessions", "list"])
    assert result.exit_code == 0, result.stdout


def test_doctor_flags_a_named_folder_the_state_is_not_in(runner, dirs, monkeypatch):
    _home, root, _link_dir = dirs
    monkeypatch.setenv("BOOKING_AGENT_PERSISTENT_ROOT", str(root))

    body = json.loads(runner.invoke(app, ["runtime", "doctor"]).stdout)

    assert body["data"]["healthy"] is False
    assert {f["kind"] for f in body["data"]["findings"]} >= {"configured_root_not_applied"}
    assert body["next_command"] == f"booking-agent runtime prepare --persistent-root {root}"


# --- `flow init` makes the agent say how this runtime keeps state (it used to be skippable) -------

@pytest.fixture
def undeclared(dirs, monkeypatch):
    """A runtime that has not said how it keeps files."""
    monkeypatch.delenv("BOOKING_AGENT_PERSISTENCE", raising=False)
    monkeypatch.delenv("BOOKING_AGENT_PERSISTENT_ROOT", raising=False)
    return dirs


def _gate(persistent_root=None, no_persistent_root=False):
    import typer

    from booking_agent_cli.commands.flow import _resolve_persistence

    try:
        _resolve_persistence(persistent_root, no_persistent_root)
    except typer.Exit:
        return False
    return True


def _error_code(capsys):
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):])["data"]["error"]["code"]


def test_init_refuses_until_the_agent_says_how_state_is_kept(undeclared, capsys):
    assert _gate() is False
    assert _error_code(capsys) == "persistence_unresolved"


def test_a_folder_the_agent_names_is_set_up_and_then_not_asked_again(undeclared, tmp_path):
    home, _root, _link_dir = undeclared
    keep = tmp_path / "survives"

    assert _gate(persistent_root=str(keep)) is True

    assert (home / ".booking-agent").is_symlink()
    assert (home / ".booking-agent").resolve() == (keep / "booking-agent").resolve()
    assert _gate() is True  # state is in a linked folder now


def test_saying_there_is_no_folder_is_remembered_beside_the_state(undeclared):
    home, _root, _link_dir = undeclared

    assert _gate(no_persistent_root=True) is True

    assert (home / ".booking-agent" / ".persistence-none").is_file()
    assert _gate() is True


def test_a_fresh_runtime_that_lost_the_note_is_asked_again(undeclared):
    home, _root, _link_dir = undeclared
    assert _gate(no_persistent_root=True) is True
    (home / ".booking-agent" / ".persistence-none").unlink()
    assert _gate() is False


def test_a_host_that_keeps_files_itself_is_never_asked(undeclared, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_PERSISTENCE", "none")
    assert _gate() is True


def test_both_flags_together_are_refused(undeclared, tmp_path, capsys):
    assert _gate(persistent_root=str(tmp_path / "x"), no_persistent_root=True) is False
    assert _error_code(capsys) == "persistence_flags_conflict"


def test_a_folder_that_cannot_be_used_says_how_to_go_on(undeclared, capsys):
    assert _gate(persistent_root="relative/folder") is False
    assert _error_code(capsys) == "persistence_setup_failed"


def test_init_through_the_command_line_refuses_then_goes_on(runner, isolated_session, monkeypatch):
    monkeypatch.delenv("BOOKING_AGENT_PERSISTENCE", raising=False)
    refused = runner.invoke(app, ["flow", "init", "--user-request", "Fix my AC"])
    assert refused.exit_code == 1
    assert json.loads(refused.stdout[refused.stdout.index("{"):])["data"]["error"]["code"] == "persistence_unresolved"

    ok = runner.invoke(app, ["flow", "init", "--user-request", "Fix my AC", "--no-persistent-root"])
    assert ok.exit_code == 0, ok.stdout
