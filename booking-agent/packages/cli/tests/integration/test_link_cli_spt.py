"""Tests for shared_payment_token extraction from link-cli spend-request retrieve."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from booking_agent_cli import link_cli

# Shape from `link-cli spend-request retrieve ... --include shared_payment_token` for
# lsrq_1TjzIkJc4ah4D2aAIPMXwZUb (SPT expired; ids only, no live credential).
_LSRQ_SAMPLE_ID = "lsrq_1TjzIkJc4ah4D2aAIPMXwZUb"
_SPT_SAMPLE_ID = "spt_1TjzJHBfQCrp7lQIW7Vuk0DW"
_REAL_LINK_CLI_SPT_RETRIEVE_JSON = json.dumps(
    [
        {
            "id": _LSRQ_SAMPLE_ID,
            "status": "succeeded",
            "credential_type": "shared_payment_token",
            "shared_payment_token": {
                "id": _SPT_SAMPLE_ID,
                "valid_until": "2026-06-19T22:05:30Z",
            },
        }
    ]
)


def test_extract_spt_string_field() -> None:
    sr = {"id": "lsrq_1", "status": "approved", "shared_payment_token": "spt_test123"}
    assert link_cli._extract_shared_payment_token_from_spend_payload(sr) == "spt_test123"


def test_extract_spt_nested_object() -> None:
    sr = {
        "status": "approved",
        "shared_payment_token": {"token": "spt_nested"},
    }
    assert link_cli._extract_shared_payment_token_from_spend_payload(sr) == "spt_nested"


def test_extract_spt_credential_envelope() -> None:
    sr = {"status": "approved", "credential": {"token": "spt_cred"}}
    assert link_cli._extract_shared_payment_token_from_spend_payload(sr) == "spt_cred"


def test_extract_rejects_non_approved() -> None:
    with pytest.raises(RuntimeError, match="usable SPT"):
        link_cli._extract_shared_payment_token_from_spend_payload(
            {"status": "pending_approval", "shared_payment_token": "x"}
        )


def test_extract_spt_real_link_cli_payload_shape() -> None:
    """Regression: link-cli nests the token under shared_payment_token.id (not .token)."""
    payload = json.loads(_REAL_LINK_CLI_SPT_RETRIEVE_JSON)
    assert isinstance(payload, list)
    sr = payload[0]
    assert sr["id"] == _LSRQ_SAMPLE_ID
    assert link_cli._extract_shared_payment_token_from_spend_payload(sr) == _SPT_SAMPLE_ID


def test_retrieve_spt_parses_real_link_cli_json_shape() -> None:
    mock_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout=_REAL_LINK_CLI_SPT_RETRIEVE_JSON, stderr="")
    )
    with patch.object(link_cli.subprocess, "run", mock_run):
        assert (
            link_cli.retrieve_spend_request_shared_payment_token(_LSRQ_SAMPLE_ID)
            == _SPT_SAMPLE_ID
        )
    cmd = mock_run.call_args[0][0]
    assert _LSRQ_SAMPLE_ID in cmd


def test_extract_rejects_terminal() -> None:
    with pytest.raises(RuntimeError, match="terminal"):
        link_cli._extract_shared_payment_token_from_spend_payload(
            {"status": "denied", "shared_payment_token": "x"}
        )


def test_retrieve_spt_invokes_link_cli_with_expected_args() -> None:
    stdout = '{"id":"lsrq_x","status":"approved","shared_payment_token":"spt_from_cli"}'
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=stdout, stderr=""))
    with patch.object(link_cli.subprocess, "run", mock_run):
        assert link_cli.retrieve_spend_request_shared_payment_token("lsrq_x") == "spt_from_cli"
    args, kwargs = mock_run.call_args
    assert kwargs["capture_output"] is True
    cmd = args[0]
    assert "spend-request" in cmd
    assert "retrieve" in cmd
    assert "lsrq_x" in cmd
    assert "--interval" not in cmd
    assert "--max-attempts" not in cmd
    assert "--include" in cmd and cmd[cmd.index("--include") + 1] == "shared_payment_token"
    assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "json"


def test_retrieve_spt_retries_approved_spend_until_token_is_ready() -> None:
    approved_without_token = MagicMock(
        returncode=0,
        stdout='{"id":"lsrq_x","status":"approved"}',
        stderr="",
    )
    approved_with_token = MagicMock(
        returncode=0,
        stdout='{"id":"lsrq_x","status":"approved","shared_payment_token":"spt_ready"}',
        stderr="",
    )
    mock_run = MagicMock(side_effect=[approved_without_token, approved_with_token])

    with patch.object(link_cli.subprocess, "run", mock_run):
        token = link_cli.retrieve_spend_request_shared_payment_token(
            "lsrq_x",
            retry_interval=0,
        )

    assert token == "spt_ready"
    assert mock_run.call_count == 2


def test_retrieve_spt_does_not_retry_terminal_spend() -> None:
    mock_run = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout='{"id":"lsrq_x","status":"denied"}',
            stderr="",
        )
    )

    with (
        patch.object(link_cli.subprocess, "run", mock_run),
        pytest.raises(RuntimeError, match="terminal status: denied"),
    ):
        link_cli.retrieve_spend_request_shared_payment_token(
            "lsrq_x",
            retry_interval=0,
        )

    assert mock_run.call_count == 1


def test_retrieve_spt_stops_when_approved_token_deadline_expires() -> None:
    mock_run = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout='{"id":"lsrq_x","status":"approved"}',
            stderr="",
        )
    )

    with (
        patch.object(link_cli.subprocess, "run", mock_run),
        pytest.raises(
            link_cli.SharedPaymentTokenUnavailable,
            match="lsrq_x as approved but returned no shared_payment_token",
        ),
    ):
        link_cli.retrieve_spend_request_shared_payment_token(
            "lsrq_x",
            retry_timeout=0,
            retry_interval=0,
        )

    assert mock_run.call_count == 1


def test_create_spend_request_omits_merchant_fields_for_spt() -> None:
    mock_run = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout='{"id":"lsrq_spt","approval_url":"https://link.test/approve"}',
            stderr="",
        )
    )
    with patch.object(link_cli.subprocess, "run", mock_run):
        link_cli.create_spend_request(
            payment_method_id="pm_1",
            amount=100,
            currency="USD",
            merchant_name="Koby's HVAC",
            merchant_url="http://example.test/usp",
            context="x" * 100,
            line_item_label="AC Repair",
            credential_type="shared_payment_token",
            network_id="ntwrk_1",
        )
    cmd = mock_run.call_args[0][0]
    assert "--merchant-name" not in cmd
    assert "--merchant-url" not in cmd
    assert "--credential-type" in cmd
    assert cmd[cmd.index("--credential-type") + 1] == "shared_payment_token"
    assert "--network-id" in cmd
    assert cmd[cmd.index("--network-id") + 1] == "ntwrk_1"
    assert "--test" not in cmd


@pytest.mark.parametrize(
    "network_id, credential_type, expect_test",
    [
        ("profile_test_61VUl9MDBOhwqNj4sA6VUl9LErSQsuPYNPEwtZA36IYC", "shared_payment_token", True),
        ("profile_61VUlDnfmNwy6L05KA6VUlDmGISQWVD1WK74ZCANM1AO", "shared_payment_token", False),
        ("profile_test_61VUl9MDBOhwqNj4sA6VUl9LErSQsuPYNPEwtZA36IYC", None, False),
    ],
)
def test_create_spend_request_uses_test_mode_only_for_a_test_profile(
    network_id, credential_type, expect_test,
) -> None:
    mock_run = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout='{"id":"lsrq_spt","approval_url":"https://link.test/approve"}',
            stderr="",
        )
    )
    with patch.object(link_cli.subprocess, "run", mock_run):
        link_cli.create_spend_request(
            payment_method_id="pm_1",
            amount=110,
            currency="USD",
            merchant_name="USP Cashier",
            merchant_url="https://example.test/.well-known/ucp",
            context="x" * 100,
            line_item_label="Home HVAC Repair",
            credential_type=credential_type,
            network_id=network_id,
        )
    assert ("--test" in mock_run.call_args[0][0]) is expect_test


# Issuance 3DS (spec #93 A4): shape of link-cli spend-request JSON with a next_action.
def _requires_action(action_type: str, resolution: str, *, sr_id: str = "lsrq_3ds") -> dict:
    return {
        "id": sr_id,
        "status": "requires_action",
        "status_details": {
            "requires_action": {
                "next_action": {
                    "type": action_type,
                    "resolution": resolution,
                    "display_message": "Your bank needs to verify this payment.",
                    "action_url": "https://app.link.com/3ds/verify",
                }
            }
        },
    }


def _three_ds() -> dict:
    return _requires_action("three_d_secure", "auto_resume")


def _run_returning(*payloads: dict) -> MagicMock:
    return MagicMock(
        side_effect=[
            MagicMock(returncode=0, stdout=json.dumps(p), stderr="") for p in payloads
        ]
    )


def test_parse_next_action_reads_nested_requires_action() -> None:
    action = link_cli.parse_next_action(_three_ds())
    assert action == link_cli.SpendNextAction(
        type="three_d_secure",
        resolution="auto_resume",
        display_message="Your bank needs to verify this payment.",
        action_url="https://app.link.com/3ds/verify",
    )
    assert action.auto_resume
    assert action.key("lsrq_3ds") == "lsrq_3ds:three_d_secure"


def test_parse_next_action_absent() -> None:
    assert link_cli.parse_next_action({"status": "pending_approval"}) is None
    assert link_cli.parse_next_action(
        {"status": "requires_action", "status_details": {"requires_action": {}}}
    ) is None


def test_create_spend_request_exposes_issuance_action() -> None:
    mock_run = _run_returning(_three_ds())
    with patch.object(link_cli.subprocess, "run", mock_run):
        created = link_cli.create_spend_request(
            payment_method_id="pm_1",
            amount=100,
            currency="USD",
            merchant_name="m",
            merchant_url="u",
            context="x" * 100,
            line_item_label="AC Repair",
            credential_type="shared_payment_token",
            network_id="ntwrk_1",
        )
    assert created.status == "requires_action"
    assert created.next_action is not None
    assert created.next_action.type == "three_d_secure"


def test_wait_raises_action_required_for_unsurfaced_three_ds() -> None:
    mock_run = _run_returning(_three_ds())
    with (
        patch.object(link_cli.subprocess, "run", mock_run),
        pytest.raises(link_cli.SpendActionRequired) as caught,
    ):
        link_cli.wait_for_spend_approval("lsrq_3ds", deadline_ts=9e12, poll_interval=0)
    assert caught.value.action.action_url == "https://app.link.com/3ds/verify"
    assert mock_run.call_count == 1


@pytest.mark.parametrize("final_status", ["submitted", "succeeded", "approved"])
def test_wait_resumes_same_request_after_surfaced_three_ds(final_status) -> None:
    mock_run = _run_returning(
        _three_ds(), _three_ds(), {"id": "lsrq_3ds", "status": final_status},
    )
    with patch.object(link_cli.subprocess, "run", mock_run):
        sr = link_cli.wait_for_spend_approval(
            "lsrq_3ds",
            deadline_ts=9e12,
            poll_interval=0,
            surfaced_action_key="lsrq_3ds:three_d_secure",
        )
    assert sr.status == final_status
    assert mock_run.call_count == 3
    for call in mock_run.call_args_list:
        assert "lsrq_3ds" in call[0][0]
        assert "create" not in call[0][0]


def test_wait_raises_for_three_ds_retry_even_after_surfacing() -> None:
    mock_run = _run_returning(
        _three_ds(), _requires_action("three_d_secure_retry", "create_new_spend_request"),
    )
    with (
        patch.object(link_cli.subprocess, "run", mock_run),
        pytest.raises(link_cli.SpendActionRequired) as caught,
    ):
        link_cli.wait_for_spend_approval(
            "lsrq_3ds",
            deadline_ts=9e12,
            poll_interval=0,
            surfaced_action_key="lsrq_3ds:three_d_secure",
        )
    assert caught.value.action.type == "three_d_secure_retry"
    assert not caught.value.action.auto_resume


def test_wait_still_fails_on_terminal_status() -> None:
    mock_run = _run_returning(_three_ds(), {"id": "lsrq_3ds", "status": "failed"})
    with (
        patch.object(link_cli.subprocess, "run", mock_run),
        pytest.raises(RuntimeError, match="failed"),
    ):
        link_cli.wait_for_spend_approval(
            "lsrq_3ds",
            deadline_ts=9e12,
            poll_interval=0,
            surfaced_action_key="lsrq_3ds:three_d_secure",
        )


def test_wait_treats_requires_action_without_next_action_as_error() -> None:
    mock_run = _run_returning({"id": "lsrq_3ds", "status": "requires_action"})
    with (
        patch.object(link_cli.subprocess, "run", mock_run),
        pytest.raises(RuntimeError, match="no next_action"),
    ):
        link_cli.wait_for_spend_approval("lsrq_3ds", deadline_ts=9e12, poll_interval=0)


def test_extract_spt_from_submitted_after_issuance_three_ds() -> None:
    sr = {"status": "submitted", "shared_payment_token": {"id": "spt_after_3ds"}}
    assert link_cli._extract_shared_payment_token_from_spend_payload(sr) == "spt_after_3ds"
