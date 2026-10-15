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
from booking_agent_cli import config_loader
from booking_agent_cli.main import app

_PACKAGES = Path(__file__).resolve().parents[3]
_SOURCE_DIRS = [
    _PACKAGES / "cli" / "src",
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
    for name in (
        "USP_PLATFORM_NAME", "USP_PLATFORM_CONTACT", "USP_REGISTRY_URL", "LINK_CLI_BIN",
        *settings.RUNTIME_ONLY,
    ):
        monkeypatch.delenv(name, raising=False)
    return session_dir, work


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_layers_fill_in_priority_order(home, monkeypatch):
    session_dir, work = home
    _write(session_dir / ".env", "USP_PLATFORM_NAME=user\nLINK_CLI_BIN=user\nUSP_REGISTRY_URL=user\n")
    _write(work / ".env", "USP_REGISTRY_URL=folder\nBOOKING_AGENT_MAX_SESSIONS=7\n")
    monkeypatch.setenv("USP_PLATFORM_NAME", "process")

    config_loader.load()

    assert settings.get("USP_PLATFORM_NAME") == "process"
    assert settings.get("LINK_CLI_BIN") == "user"
    assert settings.get("USP_REGISTRY_URL") == "user"
    assert settings.get_int("BOOKING_AGENT_MAX_SESSIONS") == 7
    assert settings.get("BOOKING_AGENT_GEOCODER_URL") == "https://nominatim.openstreetmap.org/search"
    assert config_loader.source_of("USP_PLATFORM_NAME") == config_loader.SOURCE_PROCESS
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


def test_runtime_only_names_in_an_env_file_are_ignored(home):
    session_dir, work = home
    _write(work / ".env", "BOOKING_AGENT_BUYER_DEVICE_IP=198.51.100.7\n")

    config_loader.load()

    assert "BOOKING_AGENT_BUYER_DEVICE_IP" not in os.environ
    assert config_loader.unknown_keys() == []
    assert [n for _s, _p, n in config_loader.ignored_runtime_keys()] == [
        "BOOKING_AGENT_BUYER_DEVICE_IP",
    ]


def test_a_calendar_token_in_an_env_file_is_never_used(home, monkeypatch):
    from booking_agent_cli.calendar_connection import host_token

    session_dir, _work = home
    monkeypatch.delenv("HOST_CALENDAR_TOKEN", raising=False)
    _write(session_dir / ".env", "HOST_CALENDAR_TOKEN=leaked\n")
    config_loader.load()

    assert host_token("HOST_CALENDAR_TOKEN") is None
    assert [n for _s, _p, n in config_loader.unknown_keys()] == ["HOST_CALENDAR_TOKEN"]


def test_init_reads_the_folder_env(runner, isolated_session, monkeypatch):
    monkeypatch.delenv("USP_PLATFORM_CONTACT", raising=False)
    _write(Path.cwd() / ".env", "USP_PLATFORM_CONTACT=from-folder@example.com\n")
    r = runner.invoke(app, ["flow", "init", "--user-request", "haircut"])
    assert r.exit_code == 0, r.stdout
    assert settings.get("USP_PLATFORM_CONTACT") == "from-folder@example.com"


@pytest.mark.parametrize("home_var", ["HOME", "USERPROFILE"])
def test_session_dir_default_expands_to_the_user_home(tmp_path, monkeypatch, home_var):
    monkeypatch.delenv("BOOKING_AGENT_SESSION_DIR", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setenv(home_var, str(tmp_path))
    if home_var == "USERPROFILE" and os.name != "nt":
        pytest.skip("USERPROFILE is the home only on Windows")
    assert settings.shipped_default("BOOKING_AGENT_SESSION_DIR") == "~/.booking-agent"
    assert settings.booking_agent_home() == tmp_path / ".booking-agent"


def test_session_dir_override_expands_and_must_be_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", "~/elsewhere")
    assert settings.booking_agent_home() == tmp_path / "elsewhere"

    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", "relative/dir")
    with pytest.raises(ValueError, match="BOOKING_AGENT_SESSION_DIR"):
        settings.booking_agent_home()


def test_google_settings_are_read_when_used_not_at_import(monkeypatch):
    from booking_agent.calendar import google as gcal

    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "client-after-import")
    monkeypatch.setenv("GOOGLE_CALENDAR_REDIRECT_PORT", "9123")
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


def test_defaults_file_lists_exactly_the_variables_the_code_reads():
    read = _names_read_in_source()
    listed = settings.known_names()
    assert sorted(read - listed) == [], "add these to defaults.env (or RUNTIME_ONLY)"
    assert sorted(listed - read) == [], "nothing reads these; remove them from defaults.env"
    assert {e.name for e in settings.entries()} & settings.RUNTIME_ONLY == set()


def test_shipped_defaults_match_the_code_constants():
    from booking_agent.platform_profile import DEFAULT_PLATFORM_PROFILE_URL
    from booking_agent.registry_url import DEFAULT_REGISTRY_URL
    from booking_agent_cli.session import DEFAULT_MAX_SESSIONS

    assert settings.shipped_default("USP_AGENT_PROFILE_URL") == DEFAULT_PLATFORM_PROFILE_URL
    assert settings.shipped_default("USP_REGISTRY_URL") == DEFAULT_REGISTRY_URL
    assert int(settings.shipped_default("BOOKING_AGENT_MAX_SESSIONS")) == DEFAULT_MAX_SESSIONS


def test_defaults_file_ships_inside_the_package():
    assert settings.DEFAULTS.is_file() and settings.DEFAULTS.parent.name == "booking_agent"
    import booking_agent_cli

    assert not (Path(booking_agent_cli.__file__).parent / "defaults.env").exists()


def test_config_show_hides_secrets_and_flags_unknown_keys(runner, home, monkeypatch):
    session_dir, _work = home
    _write(session_dir / ".env", "GOOGLE_CALENDAR_CLIENT_SECRET=s3cr3t-value\nOLD_RENAMED_SETTING=x\n")
    from booking_agent_cli.calendar_connection import remember_route

    remember_route("connector", "HOST_CALENDAR_TOKEN")
    monkeypatch.setenv("HOST_CALENDAR_TOKEN", "token-value")

    r = runner.invoke(app, ["config", "show"])
    assert r.exit_code == 0, r.stdout
    assert "s3cr3t-value" not in r.stdout and "token-value" not in r.stdout
    report = json.loads(r.stdout)
    by_name = {v["name"]: v for v in report["variables"]}
    assert by_name["GOOGLE_CALENDAR_CLIENT_SECRET"]["value"] == "<set, hidden>"
    assert by_name["GOOGLE_CALENDAR_CLIENT_SECRET"]["source"] == config_loader.SOURCE_USER
    assert by_name["USP_REGISTRY_URL"]["source"] in (
        config_loader.SOURCE_SHIPPED, config_loader.SOURCE_PROCESS,
    )
    assert [k["name"] for k in report["unknown_keys"]] == ["OLD_RENAMED_SETTING"]
    assert report["ignored_runtime_keys"] == []
    assert "HOST_CALENDAR_TOKEN" not in by_name
    assert report["runtime"]["calendar"] == "connected, token from host environment"


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
    assert "BOOKING_AGENT_SESSION_DIR" not in text
    assert not [line for line in text.splitlines() if line and not line.startswith("#")]

    path.write_text("USP_PLATFORM_NAME=mine\n")
    r = runner.invoke(app, ["config", "init"])
    assert json.loads(r.stdout)["written"] is False
    assert path.read_text() == "USP_PLATFORM_NAME=mine\n"


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
