"""Loopback UCP: one checkout per booking, opened for the summary and completed only after the yes."""
from __future__ import annotations

import json

import pytest

import e2e.test_ucp_skill_flow_e2e as skill_flow
from e2e.test_ucp_skill_flow_e2e import (
    _drive_offline_ucp_to_payment_pending,
    _no_link_payment,
    _offline_env,
    _stdout_json,
    _stub_link_spend_request,
)

_CONSENT_STOPS = {"booking_confirmation", "policy_acceptance"}


@pytest.fixture
def outputs(monkeypatch):
    """Every JSON object any CLI command printed, in order."""
    seen: list = []
    real = skill_flow._invoke

    def recording(runner, *args):
        result = real(runner, *args)
        text = (result.stdout or "").strip()
        dec = json.JSONDecoder()
        idx = 0
        while True:
            idx = text.find("{", idx)
            if idx == -1:
                break
            obj, idx = dec.raw_decode(text, idx)
            seen.append(obj)
        return result

    monkeypatch.setattr(skill_flow, "_invoke", recording)
    return seen


def _stack(request, tmp_path, monkeypatch, **kwargs):
    stack = _offline_env(tmp_path, monkeypatch, **kwargs)
    request.addfinalizer(stack.stop)
    return stack


def _assert_one_question(outputs):
    stops = [o.get("awaiting_user") for o in outputs if o.get("awaiting_user") in _CONSENT_STOPS]
    assert len(stops) <= 1, stops
    assert '"total_changed": true' not in json.dumps(outputs)


@pytest.mark.parametrize("route", ["at_booking", "deposit_required"])
def test_charge_now_and_deposit_use_the_one_checkout_the_summary_showed(
    runner, request, tmp_path, monkeypatch, outputs, route,
):
    stack = _stack(request, tmp_path, monkeypatch, deposit=route == "deposit_required")
    captured: dict = {}
    _service_id, book = _drive_offline_ucp_to_payment_pending(runner, stack, captured=captured)
    resolved = captured["resolve"]
    assert resolved["data"]["payment_route"] == route
    assert len(stack.checkout_store.create_bodies) == 1
    assert stack.checkout_store.complete_bodies == []
    assert book.get("awaiting_user") is None
    if route == "deposit_required":
        assert resolved["data"]["checkout"]["payment_schedules"]

    _stub_link_spend_request(monkeypatch, f"lsrq_{route}", {})
    r = skill_flow._invoke(runner, "flow", "pay", "request", "--payment-method-id", "pm_test")
    assert r.exit_code == 0, r.stdout + r.stderr
    r = skill_flow._invoke(runner, "flow", "pay", "poll")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["phase"] == "CONFIRMED"
    assert len(stack.checkout_store.create_bodies) == 1
    assert len(stack.checkout_store.complete_bodies) == 1
    assert stack.checkout_store.canceled == []
    _assert_one_question(outputs)


def test_pay_at_service_uses_the_one_checkout_the_summary_showed(
    runner, request, tmp_path, monkeypatch, outputs,
):
    stack = _stack(request, tmp_path, monkeypatch, pay_at_service=True)
    _no_link_payment(monkeypatch)
    _drive_offline_ucp_to_payment_pending(runner, stack)
    assert len(stack.checkout_store.create_bodies) == 1
    assert stack.checkout_store.complete_bodies == []
    r = skill_flow._invoke(runner, "flow", "pay", "ucp-complete")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert _stdout_json(r)["phase"] == "CONFIRMED"
    assert len(stack.checkout_store.create_bodies) == 1
    _assert_one_question(outputs)


def test_saying_no_cancels_the_checkout_at_the_business(runner, request, tmp_path, monkeypatch):
    from booking_agent_cli.session import load_session

    from booking_agent_cli.session import load_store, save_store

    stack = _stack(request, tmp_path, monkeypatch)
    run = skill_flow._invoke

    def ok(*args):
        r = run(runner, *args)
        assert r.exit_code == 0, r.stdout + r.stderr
        return _stdout_json(r)

    ok("flow", "init", "--user-request", skill_flow._USER_REQUEST, "--force")
    found = ok("flow", "discover", "--query", "AC repair Yuma")["data"]["services"][0]
    used = ok("flow", "business", "use", "--profile-url", found["profile_url"],
              "--service-id", found["service_id"])
    if "use-ucp-checkout-mode" in (used.get("next_command") or ""):
        ok("flow", "use-ucp-checkout-mode")
    run(runner, "flow", "calendar", "ask")
    run(runner, "flow", "calendar", "skip")
    ok("flow", "availability", "--service-id", found["service_id"])
    slot_id = ok("flow", "slots", "list")["data"]["slots"][0]["slot_id"]
    ok("flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    store = load_store()
    store.sessions[0].link_auth_gate_completed = True
    save_store(store)
    ok("flow", "buyer", "resolve")
    ok("flow", "book", "decline")

    [checkout_id] = stack.checkout_store.create_keys.values()
    assert stack.checkout_store.canceled == [checkout_id]
    assert stack.checkout_store.complete_bodies == []
    assert load_session().flow_phase == "BUYER_RESOLVED"
