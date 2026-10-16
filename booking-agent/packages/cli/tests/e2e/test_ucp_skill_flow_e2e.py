"""SKILL flow E2E over the local offline HTTP stack (no MockUSPBackend monkeypatch)."""
from __future__ import annotations

import json
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


def _drive_offline_ucp_to_payment_pending(runner, offline_stack):
    """Init through book on loopback UCP-native merchant; return (service_id, book_json)."""
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
    _invoke(runner, "flow", "buyer", "resolve")
    r = _invoke(runner, "flow", "book", "--service-id", service_id)
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

    r = _invoke(runner, "flow", "pay", "request", "--payment-method-id", "pm_test")
    assert r.exit_code == 0, r.stdout + r.stderr
    r = _invoke(runner, "flow", "pay", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    complete = _stdout_json(r, index=-2)
    assert complete["data"]["spt_source"] == "link-cli spend-request retrieve"
    assert complete["phase"] == "CONFIRMED"
    assert complete["data"].get("order_id")

    r = _invoke(runner, "flow", "validate-skill-compliance")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["data"]["compliant"] is True


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
    ucp_complete = next(
        o
        for o in objs
        if o.get("data", {}).get("spt_source") == "link-cli spend-request retrieve"
    )
    assert ucp_complete["phase"] == "CONFIRMED"
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
