"""The first-run installation check and the installation_unverified gate."""
from __future__ import annotations

import pytest

from booking_agent_cli import __version__ as CLI_VERSION
from booking_agent_cli import provenance
from booking_agent_cli.session import load_store, save_store
from integration.test_flow_gates import (
    _SERVICE_ID,
    _complete_checkout_mode_setup,
    _init,
    _invoke,
    _satisfy_link_auth_gate,
    _skip_calendar,
    _stdout_json,
)

_TAMPERED = provenance.IntegrityResult(
    ok=False, reason="installed_file_modified", message="booking_agent_cli/flow.py differs."
)


@pytest.fixture
def installed(monkeypatch, isolated_session):
    """The installation the check sees: its fingerprint and the next verification result."""
    state = {"fingerprint": "install-1", "result": provenance.IntegrityResult(ok=True), "checks": 0}

    def verify():
        state["checks"] += 1
        return state["result"]

    monkeypatch.setattr(provenance, "installed_fingerprint", lambda: state["fingerprint"])
    monkeypatch.setattr(provenance, "verify_installed", verify)
    return state


def _refused(result) -> dict:
    assert result.exit_code == 1, result.stdout + result.stderr
    out = _stdout_json(result)
    assert out["data"]["error"]["code"] == "installation_unverified"
    return out


def test_a_tampered_installation_cannot_start_a_booking(runner, installed):
    installed["result"] = _TAMPERED
    out = _refused(_init(runner))
    assert out["data"]["reason"] == "installed_file_modified"
    assert out["data"]["reinstall_command"] == f"uv tool install --force booking-agent-cli=={CLI_VERSION}"
    assert "their yes" in out["data"]["error"]["message"]
    assert load_store().sessions == []


def test_a_verified_installation_is_checked_once(runner, installed):
    assert _init(runner).exit_code == 0
    assert _init(runner, force=True).exit_code == 0
    assert installed["checks"] == 1
    assert load_store().install_verified == {"version": CLI_VERSION, "record_sha256": "install-1"}


def test_a_reinstall_is_checked_again(runner, installed):
    _init(runner)
    installed["fingerprint"] = "install-2"
    installed["result"] = _TAMPERED
    _refused(_init(runner, force=True))
    assert installed["checks"] == 2


def test_the_failure_is_remembered_until_a_clean_reinstall(runner, installed):
    installed["result"] = _TAMPERED
    _refused(_init(runner))
    _refused(_init(runner))
    assert installed["checks"] == 1

    installed["fingerprint"] = "install-2"
    installed["result"] = provenance.IntegrityResult(ok=True)
    assert _init(runner).exit_code == 0
    assert load_store().install_integrity_failure is None


def test_failing_to_reach_pypi_does_not_block_and_is_retried(runner, installed):
    installed["result"] = provenance.IntegrityResult(ok=False, reason="network", transient=True)
    assert _init(runner).exit_code == 0
    assert load_store().install_verified is None
    _init(runner, force=True)
    assert installed["checks"] == 2


def test_a_source_checkout_is_not_checked_or_remembered(runner, installed):
    installed["result"] = provenance.IntegrityResult(ok=True, skipped=provenance.INSTALL_EDITABLE)
    assert _init(runner).exit_code == 0
    assert load_store().install_verified is None


def _record_failure() -> None:
    store = load_store()
    store.install_integrity_failure = {
        "version": CLI_VERSION,
        "record_sha256": "install-1",
        "reason": "unexpected_file",
        "detail": "booking_agent/hook.py was not part of the release.",
    }
    store.install_verified = None
    save_store(store)


@pytest.mark.parametrize("command", [
    ("pay", "request"),
    ("pay", "poll"),
    ("pay", "ucp-complete"),
    ("pay", "rebook"),
])
def test_payment_steps_refuse_a_tampered_installation(runner, installed, command):
    _init(runner)
    _record_failure()
    out = _refused(_invoke(runner, "flow", *command))
    assert out["data"]["reason"] == "unexpected_file"


def test_booking_refuses_a_tampered_installation_before_creating_it(runner, installed):
    _init(runner)
    _invoke(runner, "flow", "prereq", "check")
    _complete_checkout_mode_setup(runner)
    _skip_calendar(runner)
    _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID)
    slots = _stdout_json(_invoke(runner, "flow", "slots", "list"))
    slot_id = slots["data"]["slots"][0]["slot_id"]
    _invoke(runner, "flow", "slots", "select", "--slot-id", slot_id, "--user-confirmed")
    _satisfy_link_auth_gate()
    _invoke(runner, "flow", "buyer", "resolve")
    _record_failure()

    _refused(_invoke(runner, "flow", "book", "--service-id", _SERVICE_ID))
    session = load_store().sessions[0]
    assert session.booking.id is None
    assert session.flow_phase != "BOOKED"


def test_the_check_results_survive_a_save_and_load(installed):
    _record_failure()
    store = load_store()
    store.install_verified = {"version": "0.0.1", "record_sha256": "old"}
    save_store(store)
    loaded = load_store()
    assert loaded.install_integrity_failure["reason"] == "unexpected_file"
    assert loaded.install_verified == {"version": "0.0.1", "record_sha256": "old"}
