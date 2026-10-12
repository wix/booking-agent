"""Layered configuration: env files, shipped defaults, and the config commands."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from booking_agent import settings
from booking_agent_cli import CLI_DEFAULTS, config_loader
from booking_agent_cli.main import app

_PACKAGES = Path(__file__).resolve().parents[2]
_SOURCE_DIRS = [
    _PACKAGES / "cli" / "src",
    _PACKAGES / "usp-core" / "src",
    _PACKAGES / "superagent-cli" / "src",
]
_ENV_NAME = re.compile(
    r'"((?:BOOKING_AGENT|USP|GOOGLE_?CALENDAR|LINK_CLI)_[A-Z0-9_]+)"'
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Session dir and working folder in separate places, so each .env is its own layer."""
    session_dir = tmp_path / "home"
    work = tmp_path / "work"
    session_dir.mkdir()
    work.mkdir()
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(session_dir))
    monkeypatch.chdir(work)
    monkeypatch.delenv(config_loader.ENV_FILE_VAR, raising=False)
    for name in ("USP_PLATFORM_NAME", "USP_PLATFORM_CONTACT", "USP_PLATFORM_RETURN_URL", "LINK_CLI_BIN"):
        monkeypatch.delenv(name, raising=False)
    return session_dir, work


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_layers_fill_in_priority_order(home, tmp_path, monkeypatch):
    session_dir, work = home
    env_file = _write(tmp_path / "op.env", "USP_PLATFORM_NAME=env file\nUSP_PLATFORM_CONTACT=env file\n")
    _write(session_dir / ".env", "USP_PLATFORM_CONTACT=user\nLINK_CLI_BIN=user\nUSP_PLATFORM_RETURN_URL=user\n")
    _write(work / ".env", "USP_PLATFORM_RETURN_URL=folder\nBOOKING_AGENT_MAX_SESSIONS=7\n")
    monkeypatch.setenv("USP_PLATFORM_NAME", "process")

    config_loader.load(env_file=str(env_file))

    assert settings.get("USP_PLATFORM_NAME") == "process"
    assert settings.get("USP_PLATFORM_CONTACT") == "env file"
    assert settings.get("LINK_CLI_BIN") == "user"
    assert settings.get("USP_PLATFORM_RETURN_URL") == "user"
    assert settings.get_int("BOOKING_AGENT_MAX_SESSIONS") == 7
    assert settings.get("BOOKING_AGENT_GEOCODER_URL") == "https://nominatim.openstreetmap.org/search"
    assert config_loader.source_of("USP_PLATFORM_NAME") == config_loader.SOURCE_PROCESS
    assert config_loader.source_of("USP_PLATFORM_CONTACT") == config_loader.SOURCE_ENV_FILE
    assert config_loader.source_of("LINK_CLI_BIN") == config_loader.SOURCE_USER
    assert config_loader.source_of("BOOKING_AGENT_MAX_SESSIONS") == config_loader.SOURCE_FOLDER
    assert config_loader.source_of("BOOKING_AGENT_GEOCODER_URL") == config_loader.SOURCE_SHIPPED


def test_reloading_drops_values_from_a_file_that_is_gone(home):
    session_dir, _work = home
    user_env = _write(session_dir / ".env", "USP_PLATFORM_NAME=user\n")
    config_loader.load()
    assert os.environ["USP_PLATFORM_NAME"] == "user"

    user_env.unlink()
    config_loader.load()
    assert "USP_PLATFORM_NAME" not in os.environ
    assert settings.get("USP_PLATFORM_NAME") == "USP Agent"


def test_preserved_restores_the_outer_config(home, tmp_path):
    session_dir, _work = home
    _write(session_dir / ".env", "USP_PLATFORM_NAME=user\n")
    env_file = _write(tmp_path / "other.env", "USP_PLATFORM_NAME=other\n")
    config_loader.load()
    with config_loader.preserved():
        config_loader.load(env_file=str(env_file))
        assert settings.get("USP_PLATFORM_NAME") == "other"
    assert settings.get("USP_PLATFORM_NAME") == "user"


def test_env_file_given_at_init_survives_later_commands(runner, isolated_session, tmp_path):
    env_file = _write(tmp_path / "booking.env", "USP_PLATFORM_CONTACT=from-init@example.com\n")
    r = runner.invoke(app, [
        "flow", "init", "--user-request", "haircut", "--env-file", str(env_file),
    ])
    assert r.exit_code == 0, r.stdout

    config_loader.reset()
    assert settings.get("USP_PLATFORM_CONTACT") == "support@booking-agent.dev"
    r = runner.invoke(app, ["flow", "status"])
    assert r.exit_code == 0, r.stdout
    assert settings.get("USP_PLATFORM_CONTACT") == "from-init@example.com"

    from booking_agent_cli.session import must_load

    assert must_load().env_file == str(env_file.resolve())


def test_env_file_variable_is_used_and_saved_at_init(runner, isolated_session, tmp_path, monkeypatch):
    env_file = _write(tmp_path / "host.env", "USP_PLATFORM_NAME=Host Agent\n")
    monkeypatch.setenv(config_loader.ENV_FILE_VAR, str(env_file))
    r = runner.invoke(app, ["flow", "init", "--user-request", "haircut"])
    assert r.exit_code == 0, r.stdout

    from booking_agent_cli.session import must_load

    assert must_load().env_file == str(env_file.resolve())
    assert settings.get("USP_PLATFORM_NAME") == "Host Agent"


def test_missing_env_file_at_init_is_refused(runner, isolated_session, tmp_path):
    r = runner.invoke(app, [
        "flow", "init", "--user-request", "haircut", "--env-file", str(tmp_path / "nope.env"),
    ])
    assert r.exit_code == 1
    body = json.loads(r.stdout)
    assert body["data"]["error"]["code"] == "env_file_not_found"
    assert body["next_command"]


def test_google_settings_are_read_when_used_not_at_import(monkeypatch):
    from booking_agent.calendar import google as gcal

    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "client-after-import")
    monkeypatch.setenv("GOOGLE_CALENDAR_REDIRECT_PORT", "9123")
    monkeypatch.delenv("GOOGLE_CALENDAR_OAUTH_REDIRECT_BASE_URL", raising=False)
    assert gcal._client_id() == "client-after-import"
    assert gcal._redirect_port() == 9123
    assert gcal._redirect_uri() == "http://localhost:9123/oauth/google/callback"


def test_every_token_dir_reader_honors_the_session_dir(tmp_path, monkeypatch):
    from booking_agent.calendar import google as gcal
    from booking_agent_cli import calendar_event, identity

    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN_CACHE_DIR", raising=False)
    expected = tmp_path / ".google-calendar-tokens"
    assert identity.google_calendar_token_cache_dir() == expected
    assert Path(calendar_event._token_cache_dir()) == expected
    assert Path(gcal._user_cache_path("cli")).parent == expected


def _names_read_in_source() -> set:
    names = set()
    for root in _SOURCE_DIRS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            names.update(_ENV_NAME.findall(path.read_text(encoding="utf-8")))
    return names


def test_defaults_files_list_exactly_the_variables_the_code_reads():
    read = _names_read_in_source()
    listed = settings.known_names()
    assert sorted(read - listed) == [], "add these to a defaults.env"
    assert sorted(listed - read) == [], "nothing reads these; remove them from defaults.env"


def test_core_defaults_cover_every_variable_core_reads():
    core_listed = {e.name for e in settings.entries() if e.file == settings.CORE_DEFAULTS}
    core_root = _PACKAGES / "usp-core" / "src"
    core_read = set()
    for path in core_root.rglob("*.py"):
        core_read.update(_ENV_NAME.findall(path.read_text(encoding="utf-8")))
    assert sorted(core_read - core_listed) == []


def test_shipped_defaults_match_the_code_constants():
    from booking_agent.platform_profile import DEFAULT_PLATFORM_PROFILE_URL
    from booking_agent.registry_url import DEFAULT_REGISTRY_URL
    from booking_agent_cli.session import DEFAULT_MAX_SESSIONS

    assert settings.shipped_default("USP_AGENT_PROFILE_URL") == DEFAULT_PLATFORM_PROFILE_URL
    assert settings.shipped_default("USP_REGISTRY_URL") == DEFAULT_REGISTRY_URL
    assert int(settings.shipped_default("BOOKING_AGENT_MAX_SESSIONS")) == DEFAULT_MAX_SESSIONS


def test_defaults_files_ship_inside_the_packages():
    assert CLI_DEFAULTS.is_file() and CLI_DEFAULTS.parent.name == "booking_agent_cli"
    assert settings.CORE_DEFAULTS.is_file() and settings.CORE_DEFAULTS.parent.name == "booking_agent"


def test_config_show_hides_secrets_and_flags_unknown_keys(runner, home, monkeypatch):
    session_dir, _work = home
    _write(session_dir / ".env", "GOOGLE_CALENDAR_CLIENT_SECRET=s3cr3t-value\nOLD_RENAMED_SETTING=x\n")
    monkeypatch.setenv("BOOKING_AGENT_UCP_MERCHANT_BEARER", "bearer-value")

    r = runner.invoke(app, ["config", "show"])
    assert r.exit_code == 0, r.stdout
    assert "s3cr3t-value" not in r.stdout and "bearer-value" not in r.stdout
    report = json.loads(r.stdout)
    by_name = {v["name"]: v for v in report["variables"]}
    assert by_name["GOOGLE_CALENDAR_CLIENT_SECRET"]["value"] == "<set, hidden>"
    assert by_name["GOOGLE_CALENDAR_CLIENT_SECRET"]["source"] == config_loader.SOURCE_USER
    assert by_name["USP_REGISTRY_URL"]["source"] in (
        config_loader.SOURCE_SHIPPED, config_loader.SOURCE_PROCESS,
    )
    assert [k["name"] for k in report["unknown_keys"]] == ["OLD_RENAMED_SETTING"]
    assert report["runtime"]["calendar"].startswith("not connected")


def test_config_init_writes_a_private_file_once(runner, home):
    session_dir, _work = home
    r = runner.invoke(app, ["config", "init"])
    assert r.exit_code == 0, r.stdout
    path = session_dir / ".env"
    assert json.loads(r.stdout) == {"written": True, "path": str(path)}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert "# GOOGLE_CALENDAR_CLIENT_ID=" in text
    assert "# BOOKING_AGENT_GEOCODER_URL=https://nominatim.openstreetmap.org/search" in text
    assert not [line for line in text.splitlines() if line and not line.startswith("#")]

    path.write_text("USP_PLATFORM_NAME=mine\n")
    r = runner.invoke(app, ["config", "init"])
    assert json.loads(r.stdout)["written"] is False
    assert path.read_text() == "USP_PLATFORM_NAME=mine\n"


def test_public_tree_publishes_both_defaults_files(tmp_path):
    script = _PACKAGES.parent / "scripts" / "build_public_tree.py"
    if not script.is_file():
        pytest.skip("public tree builder is private")
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_public_tree", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    dest = module.build(tmp_path)
    for rel in module.REQUIRED_FILES:
        assert (dest / rel).is_file(), rel
    assert module.check(tmp_path, dest) == []


_TESTS_AND_SCRIPTS_ONLY = {
    "BOOKING_AGENT_UCP_TEST_NETWORK_ID",
    "BOOKING_AGENT_STAGING_REGISTRY_TEST",
    "BOOKING_AGENT_STAGING_SEARCH_QUERY",
    "BOOKING_AGENT_STAGING_UCP_PROFILE_URL",
}


def test_env_example_names_only_known_settings():
    example = _PACKAGES.parent / ".env.example"
    if not example.is_file():
        pytest.skip(".env.example is a private root doc")
    names = set(re.findall(r"(?m)^#?\s*([A-Z][A-Z0-9_]+)=", example.read_text()))
    assert names
    assert names - settings.known_names() - _TESTS_AND_SCRIPTS_ONLY == set()
    assert _TESTS_AND_SCRIPTS_ONLY & settings.known_names() == set()
