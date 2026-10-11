"""Tests for link-auth connect and link_cli auth helpers."""
import json

import pytest

from booking_agent_cli import link_cli
from booking_agent_cli.link_cli import (
    AuthStatus,
    _parse_link_cli_json_objects,
    get_auth_status,
    initiate_auth_login,
    poll_auth_until_authenticated,
)
from booking_agent_cli.main import app
from booking_agent_cli.session import Session, SessionStore, add_session, load_store, save_store


def _invoke(runner, *args):
    return runner.invoke(app, list(args))


def _stdout_json(result, *, index: int = -1):
    dec = json.JSONDecoder()
    s = result.stdout.strip()
    if not s:
        return {}
    objs = []
    idx = 0
    while idx < len(s):
        while idx < len(s) and s[idx] not in "{[":
            nl = s.find("\n", idx)
            if nl == -1:
                return objs[index] if objs else {}
            idx = nl + 1
        if idx >= len(s):
            break
        obj, end = dec.raw_decode(s, idx)
        objs.append(obj)
        idx = end
        while idx < len(s) and s[idx].isspace():
            idx += 1
    return objs[index]


def _seed_slot_selected(tmp_path) -> Session:
    store = SessionStore()
    session = Session(
        user_request="Book AC repair",
        flow_phase="SLOT_SELECTED",
        secrets_mode="demo",
        calendar_gate_completed=True,
        checkout_prereq_completed=True,
        slot_id="slot-1",
        slot_user_confirmed=True,
    )
    add_session(store, session)
    save_store(store)
    return session


def test_parse_link_cli_json_objects_multiple():
    stdout = '[{"authenticated": false}]\n[{"authenticated": true}]'
    objs = _parse_link_cli_json_objects(stdout)
    assert len(objs) == 2
    assert objs[-1]["authenticated"] is True


def test_link_auth_connect_already_authenticated(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    _seed_slot_selected(isolated_session)
    monkeypatch.setattr(flow.shutil, "which", lambda _: "/usr/bin/link-cli")
    monkeypatch.setattr(
        flow,
        "get_auth_status",
        lambda: AuthStatus(authenticated=True),
    )

    r = _invoke(runner, "flow", "link-auth", "connect")
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["authenticated"] is True
    assert body["next_command"] == "booking-agent flow buyer resolve"

    store = load_store()
    assert store.sessions[0].link_auth_gate_completed is True


def test_link_auth_connect_emits_url_then_polls(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    _seed_slot_selected(isolated_session)
    monkeypatch.setattr(flow.shutil, "which", lambda _: "/usr/bin/link-cli")
    monkeypatch.setattr(
        flow,
        "get_auth_status",
        lambda: AuthStatus(authenticated=False),
    )
    monkeypatch.setattr(
        flow,
        "initiate_auth_login",
        lambda **_: AuthStatus(
            authenticated=False,
            verification_url="https://app.link.com/device/setup?code=test-code",
            phrase="test-phrase",
        ),
    )
    monkeypatch.setattr(flow, "poll_auth_until_authenticated", lambda **_k: AuthStatus(authenticated=True))

    r = _invoke(runner, "flow", "link-auth", "connect")
    assert r.exit_code == 0, r.stdout + r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == "https://app.link.com/device/setup?code=test-code"
    first = _stdout_json(r, index=0)
    final = _stdout_json(r, index=-1)
    assert first["awaiting_user"] == "link_auth_approval"
    assert first["user_message"] == "https://app.link.com/device/setup?code=test-code"
    assert final["data"]["authenticated"] is True
    assert final["next_command"] == "booking-agent flow buyer resolve"


def test_buyer_resolve_requires_link_auth_gate(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    _seed_slot_selected(isolated_session)
    monkeypatch.setattr(flow.shutil, "which", lambda _: "/usr/bin/link-cli")

    r = _invoke(runner, "flow", "buyer", "resolve")
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "link_auth_gate_not_completed"
    assert body["next_command"] == "booking-agent flow link-auth connect"


def test_slots_select_points_at_link_auth_connect(runner, isolated_session, monkeypatch):
    from booking_agent_cli.session import SlotRef

    store = SessionStore()
    session = Session(
        user_request="Book AC repair",
        flow_phase="SLOTS_PRESENTED",
        secrets_mode="demo",
        calendar_gate_completed=True,
        checkout_prereq_completed=True,
        slots_presented=[
            SlotRef(slot_id="slot-1", start="2026-07-16T10:00:00Z", end="2026-07-16T11:00:00Z"),
        ],
    )
    add_session(store, session)
    save_store(store)

    r = _invoke(
        runner,
        "flow",
        "slots",
        "select",
        "--slot-id",
        "slot-1",
        "--user-confirmed",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["next_command"] == "booking-agent flow link-auth connect"


def test_get_auth_status_parses_link_cli_array(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = '[{"authenticated": true, "credentials_path": "/tmp/creds"}]'
        stderr = ""

    monkeypatch.setattr(link_cli.subprocess, "run", lambda *a, **k: FakeCompleted())
    status = get_auth_status()
    assert status.authenticated is True


def test_initiate_auth_login_returns_verification_url(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = (
            '[{"verification_url": "https://app.link.com/device/setup?code=abc", '
            '"phrase": "abc-def"}]'
        )
        stderr = ""

    monkeypatch.setattr(link_cli.subprocess, "run", lambda *a, **k: FakeCompleted())
    status = initiate_auth_login(client_name="booking-agent")
    assert status.verification_url == "https://app.link.com/device/setup?code=abc"
    assert status.phrase == "abc-def"


def test_poll_auth_until_authenticated_requires_terminal_true(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = '[{"authenticated": false}]\n[{"authenticated": true}]'
        stderr = ""

    monkeypatch.setattr(link_cli.subprocess, "run", lambda *a, **k: FakeCompleted())
    status = poll_auth_until_authenticated(poll_interval=0.1, timeout=5)
    assert status.authenticated is True


def test_poll_auth_until_authenticated_timeout(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = '[{"authenticated": false}]'
        stderr = ""

    monkeypatch.setattr(link_cli.subprocess, "run", lambda *a, **k: FakeCompleted())
    with pytest.raises(TimeoutError):
        poll_auth_until_authenticated(poll_interval=0.1, timeout=5)
