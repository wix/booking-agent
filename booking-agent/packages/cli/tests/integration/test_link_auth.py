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
from booking_agent_cli.output import LINK_AUTH_POLL_BACKGROUND_INSTRUCTION
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


def _seed_slot_selected(tmp_path, *, url_surfaced: bool = True) -> Session:
    store = SessionStore()
    session = Session(
        user_request="Book AC repair",
        flow_phase="SLOT_SELECTED",
        calendar_gate_completed=True,
        checkout_prereq_completed=True,
        slot_id="slot-1",
        slot_user_confirmed=True,
        link_auth_url_surfaced=url_surfaced,
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


URL = "https://app.link.com/device/setup?code=test-code"


def _signed_out(monkeypatch, *, pending: bool = False):
    import booking_agent_cli.commands.flow as flow

    monkeypatch.setattr(flow.shutil, "which", lambda _: "/usr/bin/link-cli")
    monkeypatch.setattr(
        flow,
        "get_auth_status",
        lambda: AuthStatus(authenticated=False, verification_url=URL if pending else None),
    )
    monkeypatch.setattr(
        flow,
        "initiate_auth_login",
        lambda **_: AuthStatus(authenticated=False, verification_url=URL, phrase="test-phrase"),
    )
    return flow


def test_link_auth_connect_returns_url_without_polling(runner, isolated_session, monkeypatch):
    _seed_slot_selected(isolated_session)
    flow = _signed_out(monkeypatch)

    def _must_not_poll(**_k):
        raise AssertionError("connect must not block on approval")

    monkeypatch.setattr(flow, "poll_auth_until_authenticated", _must_not_poll)

    r = _invoke(runner, "flow", "link-auth", "connect")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[0] == URL
    body = _stdout_json(r)
    assert body["awaiting_user"] == "link_auth_approval"
    assert body["user_message"] == URL
    assert body["next_command"] == "booking-agent flow link-auth poll"
    assert LINK_AUTH_POLL_BACKGROUND_INSTRUCTION in body["prompt"]
    assert load_store().sessions[0].link_auth_gate_completed is False


def test_link_auth_connect_wait_blocks_until_approved(runner, isolated_session, monkeypatch):
    _seed_slot_selected(isolated_session)
    flow = _signed_out(monkeypatch)
    monkeypatch.setattr(flow, "poll_auth_until_authenticated", lambda **_k: AuthStatus(authenticated=True))

    r = _invoke(runner, "flow", "link-auth", "connect", "--wait")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r, index=0)["user_message"] == URL
    final = _stdout_json(r, index=-1)
    assert final["data"]["authenticated"] is True
    assert final["next_command"] == "booking-agent flow buyer resolve"


def test_link_auth_poll_waits_then_continues_flow(runner, isolated_session, monkeypatch):
    _seed_slot_selected(isolated_session)
    flow = _signed_out(monkeypatch, pending=True)
    monkeypatch.setattr(flow, "poll_auth_until_authenticated", lambda **_k: AuthStatus(authenticated=True))

    r = _invoke(runner, "flow", "link-auth", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["authenticated"] is True
    assert body["next_command"] == "booking-agent flow buyer resolve"
    assert "user_message" not in body or not body["user_message"]
    assert load_store().sessions[0].link_auth_gate_completed is True


def test_link_auth_poll_shows_a_url_the_user_never_saw(runner, isolated_session, monkeypatch):
    _seed_slot_selected(isolated_session, url_surfaced=False)
    flow = _signed_out(monkeypatch, pending=True)

    def _must_not_poll(**_k):
        raise AssertionError("poll must show the URL before waiting")

    monkeypatch.setattr(flow, "poll_auth_until_authenticated", _must_not_poll)

    r = _invoke(runner, "flow", "link-auth", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["awaiting_user"] == "link_auth_approval"
    assert body["user_message"] == URL
    assert body["next_command"] == "booking-agent flow link-auth poll"
    assert load_store().sessions[0].link_auth_url_surfaced is True


def test_link_auth_connect_marks_url_surfaced_and_approval_clears_it(runner, isolated_session, monkeypatch):
    _seed_slot_selected(isolated_session, url_surfaced=False)
    flow = _signed_out(monkeypatch)
    monkeypatch.setattr(flow, "poll_auth_until_authenticated", lambda **_k: AuthStatus(authenticated=True))

    assert _invoke(runner, "flow", "link-auth", "connect").exit_code == 0
    assert load_store().sessions[0].link_auth_url_surfaced is True
    monkeypatch.setattr(
        flow, "get_auth_status", lambda: AuthStatus(authenticated=False, verification_url=URL),
    )
    assert _invoke(runner, "flow", "link-auth", "poll").exit_code == 0
    assert load_store().sessions[0].link_auth_url_surfaced is False


def test_link_auth_poll_timeout_keeps_same_link(runner, isolated_session, monkeypatch):
    _seed_slot_selected(isolated_session)
    flow = _signed_out(monkeypatch, pending=True)

    def _timeout(**_k):
        raise TimeoutError("link_auth_timeout")

    monkeypatch.setattr(flow, "poll_auth_until_authenticated", _timeout)

    r = _invoke(runner, "flow", "link-auth", "poll")
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "link_auth_timeout"
    assert body["next_command"] == "booking-agent flow link-auth poll"
    assert not body.get("user_message")


def test_link_auth_poll_without_pending_sign_in(runner, isolated_session, monkeypatch):
    _seed_slot_selected(isolated_session)
    _signed_out(monkeypatch)

    r = _invoke(runner, "flow", "link-auth", "poll")
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "link_auth_not_started"
    assert body["next_command"] == "booking-agent flow link-auth connect"


def test_link_auth_works_without_a_session(runner, isolated_session, monkeypatch):
    flow = _signed_out(monkeypatch)
    r = _invoke(runner, "flow", "link-auth", "connect")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["user_message"] == URL

    monkeypatch.setattr(flow, "get_auth_status", lambda: AuthStatus(authenticated=False, verification_url=URL))
    monkeypatch.setattr(flow, "poll_auth_until_authenticated", lambda **_k: AuthStatus(authenticated=True))
    r = _invoke(runner, "flow", "link-auth", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    body = _stdout_json(r)
    assert body["data"]["authenticated"] is True
    assert body["next_command"] is None
    assert load_store().sessions == []


def test_init_signed_out_starts_with_link_sign_in(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    monkeypatch.setattr(flow, "get_auth_status", lambda: AuthStatus(authenticated=False))
    monkeypatch.setattr(flow, "require_fetchable_profile_url", lambda: "https://example.test/p.json")

    r = _invoke(runner, "flow", "init", "--user-request", "Fix my AC next Tuesday")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["next_command"] == "booking-agent flow link-auth connect"


def test_search_area_propose_signed_out_asks_for_sign_in(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli import link_cli as lc

    store = SessionStore()
    add_session(store, Session(user_request="Fix my AC", flow_phase="PREREQ_OK"))
    save_store(store)

    def _fail(**_k):
        raise lc.LinkCliShippingAddressError("link_cli_failed")

    monkeypatch.setattr(lc, "retrieve_shipping_addresses", _fail)
    monkeypatch.setattr(flow, "get_auth_status", lambda: AuthStatus(authenticated=False))

    r = _invoke(runner, "flow", "search-area", "propose")
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "link_auth_gate_not_completed"
    assert body["next_command"] == "booking-agent flow link-auth connect"


def test_buyer_resolve_requires_link_auth_gate(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    _seed_slot_selected(isolated_session)
    monkeypatch.setattr(flow.shutil, "which", lambda _: "/usr/bin/link-cli")
    monkeypatch.setattr(flow, "get_auth_status", lambda: AuthStatus(authenticated=False))

    r = _invoke(runner, "flow", "buyer", "resolve")
    assert r.exit_code == 1
    body = _stdout_json(r)
    assert body["data"]["error"]["code"] == "link_auth_gate_not_completed"
    assert body["next_command"] == "booking-agent flow link-auth connect"


def test_buyer_resolve_trusts_link_when_the_sign_in_poll_never_reported(
    runner, isolated_session, monkeypatch
):
    """The user approved, but the poll that records it was killed: do not ask them to sign in again."""
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.session import load_session

    _seed_slot_selected(isolated_session)
    monkeypatch.setattr(flow.shutil, "which", lambda _: "/usr/bin/link-cli")
    monkeypatch.setattr(flow, "get_auth_status", lambda: AuthStatus(authenticated=True))
    assert load_session().link_auth_gate_completed is False

    r = _invoke(runner, "flow", "buyer", "resolve")

    body = _stdout_json(r)
    assert body["data"].get("error", {}).get("code") != "link_auth_gate_not_completed"
    assert load_session().link_auth_gate_completed is True


def test_slots_select_points_at_link_auth_connect(runner, isolated_session, monkeypatch):
    from booking_agent_cli.session import SlotRef

    store = SessionStore()
    session = Session(
        user_request="Book AC repair",
        flow_phase="SLOTS_PRESENTED",
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


# --- One batched message when Link and the calendar are both gone (reset regression) -----------

def _seed_events_calendar(session_phase="SLOT_SELECTED"):
    store = SessionStore()
    session = Session(
        user_request="Book AC repair",
        flow_phase=session_phase,
        calendar_gate_completed=True,
        calendar_state="connected",
        calendar_mode="events",
        calendar_route="connector",
        calendar_token_env="GCAL_TOKEN",
        checkout_prereq_completed=True,
        slot_id="slot-1",
        slot_user_confirmed=True,
        link_auth_gate_completed=True,
    )
    add_session(store, session)
    save_store(store)
    return session


def _calendar_gone(monkeypatch):
    import booking_agent_cli.commands.flow as flow

    monkeypatch.setattr(flow, "refresh_host_token", lambda *_a, **_k: None)
    monkeypatch.setattr(flow, "calendar_connection_status", lambda *_a, **_k: {"connected": False})


def test_connections_ensure_reports_link_and_calendar_in_one_message(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    _seed_events_calendar()
    _calendar_gone(monkeypatch)
    monkeypatch.setattr(flow, "get_auth_status", lambda: AuthStatus(authenticated=False, verification_url=URL))

    r = _invoke(runner, "flow", "connections", "ensure")

    assert r.exit_code == 0, r.stdout
    body = _stdout_json(r)
    assert body["awaiting_user"] == "connections"
    assert body["user_message"] == URL
    gaps = {g["name"]: g for g in body["data"]["connections"]}
    assert gaps["link"]["state"] == "needs_sign_in"
    assert gaps["calendar"]["command"].startswith("booking-agent flow calendar connect --mode events")
    assert "GCAL_TOKEN" in gaps["calendar"]["command"]
    assert "same message" in body["prompt"]


def test_connections_ensure_is_silent_when_everything_is_connected(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow

    _seed_events_calendar()
    monkeypatch.setattr(flow, "refresh_host_token", lambda *_a, **_k: None)
    monkeypatch.setattr(flow, "calendar_connection_status", lambda *_a, **_k: {"connected": True})

    r = _invoke(runner, "flow", "connections", "ensure")

    body = _stdout_json(r)
    assert r.exit_code == 0, r.stdout
    assert body["awaiting_user"] is None
    assert body["data"]["connections"] == []


def test_buyer_resolve_catches_a_lost_calendar_before_the_confirmation(runner, isolated_session, monkeypatch):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.session import load_session

    _seed_events_calendar()
    _calendar_gone(monkeypatch)
    monkeypatch.setattr(flow.shutil, "which", lambda _: "/usr/bin/link-cli")

    r = _invoke(runner, "flow", "buyer", "resolve")

    body = _stdout_json(r)
    assert body["awaiting_user"] == "connections"
    assert "booking_recap_lines" not in body["data"]
    assert load_session().flow_phase == "SLOT_SELECTED"


def test_status_hands_back_saved_search_results_instead_of_a_new_search(runner, isolated_session):
    rows = [{"service_id": "s1", "display_line": "AC Repair, HVAC-Koby, $1.00", "profile_url": "https://b.test"}]
    store = SessionStore()
    add_session(store, Session(
        user_request="Fix my AC", flow_phase="PREREQ_OK",
        discover_services=rows, discover_query="ac repair",
    ))
    save_store(store)

    body = _stdout_json(_invoke(runner, "flow", "status"))

    assert body["awaiting_user"] == "business_choice"
    assert body["data"]["services"] == rows
    assert "do not search again" in body["prompt"]


def test_the_default_next_step_after_listing_slots_carries_the_confirmation_flag():
    from booking_agent_cli.engine import recommend_next

    session = Session(user_request="x", flow_phase="SLOTS_PRESENTED")
    assert recommend_next(session).endswith("--user-confirmed")


def test_init_warns_before_abandoning_a_session_with_a_payment_in_progress(runner, isolated_session):
    from booking_agent_cli.session import PaymentRef

    store = SessionStore()
    session = Session(user_request="Fix my AC", flow_phase="PAYMENT_PENDING")
    session.payment = PaymentRef(spend_request_id="lsrq_1")
    add_session(store, session)
    save_store(store)

    r = _invoke(runner, "flow", "init", "--user-request", "Fix my AC")

    assert r.exit_code == 1
    assert "pay twice" in _stdout_json(r)["data"]["error"]["message"]


# --- Rules that used to live only in the skill, now asked or filled in by the CLI ----------------

def test_init_asks_whether_to_resume_a_similar_unfinished_request(runner, isolated_session):
    store = SessionStore()
    unfinished = Session(user_request="Fix my AC next Tuesday please", flow_phase="PREREQ_OK")
    finished = Session(user_request="Fix my AC next Tuesday", flow_phase="CONFIRMED")
    add_session(store, unfinished)
    add_session(store, finished)  # the newest session is finished, so nothing is "active"
    save_store(store)

    r = _invoke(runner, "flow", "init", "--user-request", "Fix my AC next Tuesday")

    body = _stdout_json(r)
    assert body["awaiting_user"] == "resume_or_fresh", body
    assert body["data"]["resume_command"].endswith(unfinished.id)
    assert "--force" in body["prompt"]


def test_discover_asap_fills_in_the_current_time(runner, isolated_session, monkeypatch):
    import booking_agent.headless as headless

    seen = {}
    real = headless.build_desired_service_time_preferences

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(headless, "build_desired_service_time_preferences", spy)
    store = SessionStore()
    add_session(store, Session(user_request="Fix my AC", flow_phase="PREREQ_OK"))
    save_store(store)

    _invoke(runner, "flow", "discover", "--asap")

    assert len(seen["desired_from"]) == 1 and "T" in seen["desired_from"][0]
