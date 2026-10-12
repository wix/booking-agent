"""Unit tests for ``nonce_from_pop_error_body`` (MCP and REST 401 fallback)."""

from booking_agent.authorization import ERR_PROOF_NONCE_REQUIRED, nonce_from_pop_error_body


def test_returns_nonce_when_code_and_data_match():
    assert nonce_from_pop_error_body({
        "code": ERR_PROOF_NONCE_REQUIRED,
        "data": {"nonce": "abc123"},
    }) == "abc123"


def test_returns_none_for_wrong_code():
    assert nonce_from_pop_error_body({
        "code": "pop_proof_invalid",
        "data": {"nonce": "abc123"},
    }) is None


def test_returns_none_for_non_string_nonce():
    assert nonce_from_pop_error_body({
        "code": ERR_PROOF_NONCE_REQUIRED,
        "data": {"nonce": 42},
    }) is None


def test_returns_none_for_empty_nonce():
    assert nonce_from_pop_error_body({
        "code": ERR_PROOF_NONCE_REQUIRED,
        "data": {"nonce": ""},
    }) is None
