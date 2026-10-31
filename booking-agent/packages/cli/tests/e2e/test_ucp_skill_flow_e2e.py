"""SKILL flow E2E over the local offline HTTP stack (no MockUSPBackend monkeypatch)."""
from __future__ import annotations

import json
import shlex
from types import SimpleNamespace

import pytest

from booking_agent_cli.main import app
from support.mock.link_cli import fake_shared_payment_token

# This module must remain green when collected immediately after
# test_flow_gates.py, whose isolated_session fixture installs hermetic backend
# and UCP profile stubs.


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
        while idx < len(s) and s[idx] != "{":
            next_nl = s.find("\n", idx)
            idx = len(s) if next_nl == -1 else next_nl + 1
        if idx >= len(s):
            break
        obj, end = dec.raw_decode(s, idx)
        objs.append(obj)
        idx = end
        while idx < len(s) and s[idx].isspace():
            idx += 1
    return objs[index]


_USER_REQUEST = "find me an AC technician in Yuma"
_SERVICE_ID = "e4d4b7a4-0e66-4104-891b-ba5bb4a04092"


@pytest.fixture
def offline_http_env(tmp_path, monkeypatch):
    """Live backend pointed at loopback stack; isolated session dir."""
    from support.offline_stack.stack import OfflineHttpStack

    stack = OfflineHttpStack()
    port, base_url = stack.start()
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("USP_REGISTRY_URL", f"{base_url}/registry/search_services")
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", stack.platform_profile_url)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import AuthStatus

    monkeypatch.setattr(
        flow,
        "get_auth_status",
        lambda: AuthStatus(authenticated=True),
    )
    from support.mock.link_cli import mock_link_cli_user_info

    mock_link_cli_user_info(
        monkeypatch, {"name": "Ada Lovelace", "email": "ada@booking-agent.test"}
    )
    yield stack
    stack.stop()


@pytest.fixture
def offline_http_env_full_spt(tmp_path, monkeypatch):
    """Offline stack with a checkout network_id seed."""
    from support.offline_stack.stack import OfflineHttpStack

    monkeypatch.setenv("BOOKING_AGENT_UCP_TEST_NETWORK_ID", "ntwrk_test_demo")
    stack = OfflineHttpStack()
    port, base_url = stack.start()
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("USP_REGISTRY_URL", f"{base_url}/registry/search_services")
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", stack.platform_profile_url)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import AuthStatus

    monkeypatch.setattr(
        flow,
        "get_auth_status",
        lambda: AuthStatus(authenticated=True),
    )
    from support.mock.link_cli import mock_link_cli_user_info

    mock_link_cli_user_info(
        monkeypatch, {"name": "Ada Lovelace", "email": "ada@booking-agent.test"}
    )
    yield stack
    stack.stop()


def _stub_link_spend_request(monkeypatch, spend_request_id: str, captured: dict):
    """Approve every spend request instantly and hand back a fixed SPT."""
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import SpendRequestCreateResult, SpendRequestStatus

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SpendRequestCreateResult(
            id=spend_request_id,
            approval_url="https://link.test/approve",
            raw={"id": spend_request_id},
            command_summary="link-cli spend-request create ...",
        )

    monkeypatch.setattr(flow, "create_spend_request", fake_create)
    monkeypatch.setattr(
        flow,
        "wait_for_spend_approval",
        lambda *_a, **_k: SpendRequestStatus(id=spend_request_id, status="approved"),
    )
    fake_shared_payment_token(monkeypatch, "spt_from_link_cli")


def _drive_offline_ucp_to_payment_pending(runner, offline_stack, captured=None):
    """Init through book on loopback UCP-native merchant; return (service_id, book_json).

    The book step runs buyer resolve's own next_command, so the summary yes covers the
    checkout terms. *captured* receives the buyer resolve output under ``resolve``.
    """
    base_url = offline_stack.base_url
    r = _invoke(
        runner,
        "flow",
        "init",
        "--user-request",
        _USER_REQUEST,
        "--force",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    r = _invoke(runner, "flow", "prereq", "check")
    assert r.exit_code == 0, r.stdout + r.stderr
    r = _invoke(runner, "flow", "discover", "--query", "AC repair Yuma")
    assert r.exit_code == 0, r.stdout + r.stderr
    discover = _stdout_json(r)
    services = discover["data"]["services"]
    profile_url = services[0]["profile_url"]
    service_id = services[0]["service_id"]
    assert profile_url.startswith(base_url)
    r = _invoke(
        runner,
        "flow",
        "business",
        "use",
        "--profile-url",
        profile_url,
        "--service-id",
        service_id,
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    next_cmd = _stdout_json(r).get("next_command") or ""
    if "use-ucp-checkout-mode" in next_cmd:
        r = _invoke(runner, "flow", "use-ucp-checkout-mode")
        assert r.exit_code == 0, r.stdout + r.stderr
    _invoke(runner, "flow", "calendar", "ask")
    _invoke(runner, "flow", "calendar", "skip")
    availability_result = _invoke(
        runner, "flow", "availability", "--service-id", service_id,
    )
    assert availability_result.exit_code == 0, (
        availability_result.stdout + availability_result.stderr
    )
    slots_result = _invoke(runner, "flow", "slots", "list")
    assert slots_result.exit_code == 0, slots_result.stdout + slots_result.stderr
    slots = _stdout_json(slots_result)
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(
        runner,
        "flow",
        "slots",
        "select",
        "--slot-id",
        slot_id,
        "--user-confirmed",
    )
    from booking_agent_cli.session import load_store, save_store

    store = load_store()
    store.sessions[0].link_auth_gate_completed = True
    save_store(store)
    r = _invoke(runner, "flow", "buyer", "resolve")
    assert r.exit_code == 0, r.stdout + r.stderr
    resolved = _stdout_json(r)
    if captured is not None:
        captured["resolve"] = resolved
    assert resolved["awaiting_user"] == "booking_confirmation"
    book_args = shlex.split(resolved["next_command"])[1:]
    r = _invoke(runner, *book_args, "--service-id", service_id)
    assert r.exit_code == 0, r.stdout + r.stderr
    book = _stdout_json(r)
    assert book["data"]["checkout_path"] == "ucp"
    assert book["phase"] == "PAYMENT_PENDING"
    return service_id, book


@pytest.mark.parametrize(
    ("failed_args", "failure_message"),
    [
        (("flow", "availability"), "forced availability failure"),
        (("flow", "slots", "list"), "forced slots list failure"),
    ],
)
def test_offline_driver_surfaces_command_failure(
    runner, offline_http_env, monkeypatch, failed_args, failure_message,
):
    """A failed prerequisite command must be reported before its JSON is parsed."""
    real_invoke = _invoke

    def fail_selected_command(runner, *args):
        if args[:len(failed_args)] == failed_args:
            return SimpleNamespace(
                exit_code=1,
                stdout=failure_message,
                stderr="",
            )
        return real_invoke(runner, *args)

    monkeypatch.setattr(
        "e2e.test_ucp_skill_flow_e2e._invoke",
        fail_selected_command,
    )
    with pytest.raises(AssertionError, match=failure_message):
        _drive_offline_ucp_to_payment_pending(runner, offline_http_env)


def test_ucp_skill_flow_e2e_over_http(runner, offline_http_env, monkeypatch):
    """Drive booking-agent flow * through pay poll (which completes checkout) over loopback HTTP."""
    _service_id, _book = _drive_offline_ucp_to_payment_pending(runner, offline_http_env)
    _stub_link_spend_request(monkeypatch, "lsrq_skill_flow", {})

    r = _invoke(runner, "flow", "pay", "request", "--payment-method-id", "pm_test", "--policies-accepted")
    assert r.exit_code == 0, r.stdout + r.stderr
    r = _invoke(runner, "flow", "pay", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    # One terminal answer: the booked message, not a "checkout completed, now poll" detour.
    complete = _stdout_json(r)
    assert complete["phase"] == "CONFIRMED"
    assert complete["user_message"].startswith("You're booked")
    from booking_agent_cli.session import load_session

    assert load_session().payment.order_id

    r = _invoke(runner, "flow", "validate-skill-compliance")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["data"]["compliant"] is True


def test_checkout_terms_and_privacy_links_are_shown_without_a_second_question(
    runner, offline_http_env, monkeypatch,
):
    """Replay: checkout terms and privacy links reach the user; the summary yes covers them."""
    import support.mock.ucp_checkout_store as checkout_store

    monkeypatch.setattr(checkout_store, "checkout_links", lambda: [
        {"type": "terms_of_service", "url": "https://merchant.test/terms"},
        {"type": "privacy_policy", "url": "https://merchant.test/privacy"},
        {"rel": "continue", "url": "https://merchant.test/checkout/continue"},
    ])
    captured_resolve: dict = {}
    _service_id, book = _drive_offline_ucp_to_payment_pending(
        runner, offline_http_env, captured=captured_resolve,
    )

    resolved = captured_resolve["resolve"]
    urls = [link["url"] for link in resolved["data"]["policy_links"]]
    assert urls == ["https://merchant.test/terms", "https://merchant.test/privacy"]
    assert "every data.policy_links URL" in resolved["prompt"]
    assert book.get("awaiting_user") is None
    assert book.get("user_message") is None
    assert "--policies-accepted" not in book["next_command"]

    captured: dict = {}
    _stub_link_spend_request(monkeypatch, "lsrq_policies", captured)
    r = _invoke(runner, "flow", "pay", "request", "--payment-method-id", "pm_test")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert captured


def test_offline_full_spt_path_with_checkout_network_id(
    runner, offline_http_env_full_spt, monkeypatch,
):
    """Book, pay request (checkout network_id), then ucp-complete via mocked link-cli SPT."""
    _service_id, book = _drive_offline_ucp_to_payment_pending(
        runner, offline_http_env_full_spt,
    )
    assert book["data"]["ucp_spt_network_id"] == "ntwrk_test_demo"

    captured = {}
    _stub_link_spend_request(monkeypatch, "lsrq_full_spt", captured)

    context = "x" * 100
    r = _invoke(
        runner,
        "flow",
        "pay",
        "request",
        "--payment-method-id",
        "pm_test",
        "--context",
        context,
        "--then-complete",
        "--policies-accepted",
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    assert captured["credential_type"] == "shared_payment_token"
    assert captured["network_id"] == "ntwrk_test_demo"
    pending = _stdout_json(r, index=0)
    assert pending["data"]["approval_url"] == "https://link.test/approve"
    dec = json.JSONDecoder()
    s = r.stdout.strip()
    objs = []
    idx = 0
    while idx < len(s):
        while idx < len(s) and s[idx] != "{":
            next_nl = s.find("\n", idx)
            idx = len(s) if next_nl == -1 else next_nl + 1
        if idx >= len(s):
            break
        obj, end = dec.raw_decode(s, idx)
        objs.append(obj)
        idx = end
        while idx < len(s) and s[idx].isspace():
            idx += 1
    final = objs[-1]
    assert final["phase"] == "CONFIRMED"
    assert final["data"].get("skill_compliance_validated") is True


def test_ucp_checkout_store_injects_test_network_id(monkeypatch):
    from support.mock.ucp_checkout_store import UcpCheckoutStore
    from support.mock.ucp_server import minimal_ucp_profile

    monkeypatch.setenv("BOOKING_AGENT_UCP_TEST_NETWORK_ID", "ntwrk_injected")
    profile = minimal_ucp_profile(
        checkout_base="http://127.0.0.1:9/ucp/v1",
        usp_base="http://127.0.0.1:9/usp/v1",
    )
    store = UcpCheckoutStore(profile_doc=profile)
    created = store.create_checkout({})
    handlers = created["ucp"]["payment_handlers"]["com.stripe.payments"]
    link_inst = handlers[0]["available_instruments"][0]
    assert link_inst["type"] == "link"
    assert link_inst["config"]["network_id"] == "ntwrk_injected"


def test_offline_stack_registry_search_scores_ac_repair():
    """Registry slice returns AC Repair for the Yuma technician query."""
    from support.offline_stack.stack import OfflineHttpStack, _registry_results

    stack = OfflineHttpStack()
    port, base = stack.start()
    try:
        results = _registry_results(f"{base}/.well-known/ucp", "AC technician Yuma")
        assert results
        assert results[0].service_id == _SERVICE_ID
        assert "AC" in results[0].service_name
    finally:
        stack.stop()


def test_a_restart_and_a_changed_persistent_root_mid_booking_lose_nothing(
    runner, offline_http_env, monkeypatch, tmp_path,
):
    """Replays the recorded hosted failure offline: the runtime restarts between steps and later
    conversations name the persistent folder differently. The booking must finish with the
    same saved session, with no second state folder and no booking restarted from scratch."""
    from booking_agent_cli import runtime_prepare
    from booking_agent_cli.session import load_session

    state = tmp_path / "state"
    link_dir = tmp_path / "link-config"
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(state))
    monkeypatch.setattr(runtime_prepare, "link_cli_config_dir", lambda: link_dir)
    keep_a, keep_b = tmp_path / "keep-a", tmp_path / "keep-b"

    def prepare(root):
        r = _invoke(runner, "runtime", "prepare", "--persistent-root", str(root))
        assert r.exit_code == 0, r.stdout + r.stderr

    prepare(keep_a)
    _drive_offline_ucp_to_payment_pending(runner, offline_http_env)
    booked_id = load_session().id

    # Restart 1: fresh filesystem, and the new conversation passes the state folder itself.
    state.unlink(); link_dir.unlink()
    prepare(keep_a / "booking-agent")
    assert load_session().id == booked_id
    # Later in the same runtime a conversation names another folder altogether.
    prepare(keep_b)
    assert load_session().id == booked_id
    assert not (keep_a / "booking-agent" / "booking-agent").exists()

    _stub_link_spend_request(monkeypatch, "lsrq_reset", {})
    r = _invoke(runner, "flow", "pay", "request", "--payment-method-id", "pm_test", "--policies-accepted")
    assert r.exit_code == 0, r.stdout + r.stderr
    r = _invoke(runner, "flow", "pay", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    final = _stdout_json(r)
    assert final["phase"] == "CONFIRMED"
    assert final["user_message"].startswith("You're booked")
    assert load_session().id == booked_id


# ---------------------------------------------------------------------------
# Pay at the appointment (USP 11.3 pay_at_service): checkout, no instrument.
# ---------------------------------------------------------------------------

def _offline_env(tmp_path, monkeypatch, **stack_kwargs):
    """Offline loopback stack wired like ``offline_http_env`` but with stack options."""
    from support.offline_stack.stack import OfflineHttpStack

    stack = OfflineHttpStack(**stack_kwargs)
    _port, base_url = stack.start()
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("USP_REGISTRY_URL", f"{base_url}/registry/search_services")
    monkeypatch.setenv("USP_AGENT_PROFILE_URL", stack.platform_profile_url)
    monkeypatch.delenv("GOOGLE_CALENDAR_CLIENT_ID", raising=False)
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.link_cli import AuthStatus

    monkeypatch.setattr(flow, "get_auth_status", lambda: AuthStatus(authenticated=True))
    from support.mock.link_cli import mock_link_cli_user_info

    mock_link_cli_user_info(
        monkeypatch, {"name": "Ada Lovelace", "email": "ada@booking-agent.test"}
    )
    return stack


@pytest.fixture
def pay_at_service_env(tmp_path, monkeypatch):
    stack = _offline_env(tmp_path, monkeypatch, pay_at_service=True)
    yield stack
    stack.stop()


@pytest.fixture
def undeclared_at_service_env(tmp_path, monkeypatch):
    """An at_service service on a business that does not declare pay_at_service."""
    stack = _offline_env(tmp_path, monkeypatch, at_service=True)
    yield stack
    stack.stop()


def _no_link_payment(monkeypatch):
    """Any attempt to create a Link spend request or fetch a token is a test failure."""
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli import payment_token

    def boom(*_a, **_k):
        raise AssertionError("pay-at-service must not touch Link payment")

    monkeypatch.setattr(flow, "create_spend_request", boom)
    monkeypatch.setattr(payment_token, "shared_payment_token_source", boom)


def test_pay_at_service_books_through_checkout_with_no_instrument(
    runner, pay_at_service_env, monkeypatch,
):
    """Declared pay_at_service: checkout + order, empty ``payment``, disclosure shown first."""
    from booking_agent_cli.session import load_session

    _no_link_payment(monkeypatch)
    captured: dict = {}
    _service_id, book = _drive_offline_ucp_to_payment_pending(
        runner, pay_at_service_env, captured=captured,
    )
    # The disclosure is shown with the summary, before anything completes (USP 11.3.5 S8).
    resolved = captured["resolve"]
    assert any("Nothing is charged" in line for line in resolved["data"]["policy_lines"])
    assert [link["url"] for link in resolved["data"]["policy_links"]] == ["https://example.com/terms"]
    assert resolved["next_command"] == "booking-agent flow book --user-confirmed --policies-accepted"
    checkout = resolved["data"]["checkout"]
    assert [d["code"] for d in checkout["payment_disclosures"]] == ["payment_due_at_service"]
    assert checkout["payment_schedules"][0]["type"] == "at_service"
    assert any(
        line.startswith("Price: ") and "nothing is charged now" in line
        for line in resolved["data"]["booking_recap_lines"]
    )

    assert book["data"]["payment_route"] == "pay_at_service"
    assert book["data"]["payment_due"] == "at_service"
    assert book["data"]["ucp_spt_network_id"] is None
    assert book["next_command"] == "booking-agent flow pay ucp-complete"
    assert book.get("awaiting_user") is None
    assert pay_at_service_env.checkout_store.complete_bodies == []

    # No Link payment is requested for a booking paid at the appointment.
    r = _invoke(runner, "flow", "pay", "request", "--payment-method-id", "pm_test", "--policies-accepted")
    assert r.exit_code == 1
    assert _stdout_json(r)["data"]["error"]["code"] == "pay_not_required"

    # The summary yes covers completion: no second policy question.
    r = _invoke(runner, "flow", "pay", "ucp-complete")
    assert r.exit_code == 0, r.stdout + r.stderr
    final = _stdout_json(r)
    assert final["phase"] == "CONFIRMED"
    assert final["user_message"].startswith("You're booked")
    assert "Nothing was charged" in final["user_message"]
    # S4/S5: UCP requires the payment object, and it carries no instruments.
    bodies = pay_at_service_env.checkout_store.complete_bodies
    assert [b["payment"] for b in bodies] == [{}]
    session = load_session()
    assert session.payment.order_id
    assert session.payment.status == "not_required"
    assert session.payment.spend_request_id is None

    r = _invoke(runner, "flow", "validate-skill-compliance")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["data"]["compliant"] is True


def _confirm_on_a_later_booking_read(monkeypatch, statuses):
    """Production pay-at-service: the order confirms the booking after complete returns."""
    import booking_agent_cli.booking_poll as booking_poll
    import booking_agent_cli.commands.flow as flow
    import booking_agent.observability as observability

    reads = list(statuses)
    events: list = []

    def fetch(_session, booking_id):
        status = reads.pop(0) if len(reads) > 1 else reads[0]
        return SimpleNamespace(id=booking_id, status=SimpleNamespace(value=status))

    monkeypatch.setattr(booking_poll, "fetch_booking", fetch)
    monkeypatch.setattr(flow, "fetch_booking", fetch)
    monkeypatch.setattr(flow, "_CAPTURED_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(observability, "_event_sink", events.append)
    return events


def _pending_complete_env(runner, env, monkeypatch, statuses):
    _no_link_payment(monkeypatch)
    env.checkout_store.confirm_on_complete = False
    events = _confirm_on_a_later_booking_read(monkeypatch, statuses)
    _drive_offline_ucp_to_payment_pending(runner, env)
    r = _invoke(runner, "flow", "pay", "ucp-complete", "--policies-accepted")
    return r, events


def test_pay_at_service_waits_for_the_order_to_confirm_the_booking(
    runner, pay_at_service_env, monkeypatch,
):
    """Complete returns ``completed`` with the booking still pending; nothing was charged."""
    from booking_agent_cli.session import load_session

    r, events = _pending_complete_env(
        runner, pay_at_service_env, monkeypatch, ["pending", "pending", "confirmed"],
    )

    assert r.exit_code == 0, r.stdout + r.stderr
    final = _stdout_json(r)
    assert final["phase"] == "CONFIRMED"
    assert final["user_message"].startswith("You're booked")
    assert "Nothing was charged" in final["user_message"]
    assert "payment went through" not in r.stdout
    assert all(e["event"] != "payment_captured_booking_unconfirmed" for e in events)
    session = load_session()
    assert session.payment.status == "not_required"
    assert session.payment.order_id
    assert session.payment.spend_request_id is None
    assert session.payment.captured_unconfirmed is False

    r = _invoke(runner, "flow", "book", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert load_session().payment.status == "not_required"


def test_pay_at_service_confirmation_timeout_says_nothing_was_charged(
    runner, pay_at_service_env, monkeypatch,
):
    import booking_agent_cli.commands.flow as flow
    from booking_agent_cli.session import load_session

    monkeypatch.setattr(flow, "_PAY_AT_SERVICE_CONFIRM_TIMEOUT_S", 0.05)
    r, events = _pending_complete_env(runner, pay_at_service_env, monkeypatch, ["pending"])

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "booking_confirmation_pending"
    assert out["next_command"] == "booking-agent flow book poll --wait"
    assert "nothing was charged" in out["user_message"]
    assert all(e["event"] != "payment_captured_booking_unconfirmed" for e in events)
    session = load_session()
    assert session.payment.status == "not_required"
    assert session.payment.captured_unconfirmed is False
    assert session.payment.order_id

    _confirm_on_a_later_booking_read(monkeypatch, ["confirmed"])
    r = _invoke(runner, "flow", "book", "poll", "--wait", "--poll-interval", "0.01")
    assert r.exit_code == 0, r.stdout + r.stderr
    final = _stdout_json(r)
    assert final["phase"] == "CONFIRMED"
    assert "Nothing was charged" in final["user_message"]
    assert load_session().payment.status == "not_required"


def test_pay_at_service_canceled_while_waiting_is_not_booked(
    runner, pay_at_service_env, monkeypatch,
):
    from booking_agent_cli.session import load_session

    r, _events = _pending_complete_env(
        runner, pay_at_service_env, monkeypatch, ["pending", "canceled"],
    )

    assert r.exit_code == 1
    out = _stdout_json(r)
    assert out["data"]["error"]["code"] == "booking_not_confirmed"
    assert "Nothing was charged" in out["user_message"]
    session = load_session()
    assert session.flow_phase == "FAILED"
    assert session.payment.status == "not_required"


def test_at_service_without_the_extension_stays_on_the_direct_path(
    runner, undeclared_at_service_env, monkeypatch,
):
    """USP 7.6.1: a platform MUST NOT route at_service through an undeclared checkout."""
    _no_link_payment(monkeypatch)
    stack = undeclared_at_service_env
    r = _invoke(runner, "flow", "init", "--user-request", _USER_REQUEST, "--force")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _invoke(runner, "flow", "prereq", "check").exit_code == 0
    r = _invoke(runner, "flow", "discover", "--query", "AC repair Yuma")
    row = _stdout_json(r)["data"]["services"][0]
    r = _invoke(
        runner, "flow", "business", "use",
        "--profile-url", row["profile_url"], "--service-id", row["service_id"],
    )
    assert r.exit_code == 0, r.stdout + r.stderr
    data = _stdout_json(r)["data"]
    assert data["payment_route"] == "at_service"
    assert data["checkout_mode"] == "direct"
    assert stack.checkout_store.sessions == {}


def test_pay_at_service_fails_closed_when_the_checkout_has_no_settlement(
    runner, pay_at_service_env, monkeypatch,
):
    """Routed as pay-at-service but answered as an ordinary charge: refuse, create no booking."""
    store = pay_at_service_env.checkout_store
    original = store.create_checkout

    def without_settlement(body, **kwargs):
        session = original(body, **kwargs)
        session.pop("settlement", None)
        return session

    monkeypatch.setattr(store, "create_checkout", without_settlement)
    with pytest.raises(AssertionError, match="no settlement"):
        _drive_offline_ucp_to_payment_pending(runner, pay_at_service_env)
    assert store.complete_bodies == []


def test_business_rejects_instruments_on_an_offline_checkout(pay_at_service_env):
    """The mock business enforces S5 so a platform that sent one would be caught."""
    store = pay_at_service_env.checkout_store
    created = store.create_checkout({"currency": "USD", "booking": {"service_id": "svc"}})
    status, payload = store.complete_checkout_checked(
        created["id"],
        {"payment": {"instruments": [{"handler_id": "stripe_payments", "type": "link"}]}},
    )
    assert status == 400
    assert payload["code"] == "validation_error"
