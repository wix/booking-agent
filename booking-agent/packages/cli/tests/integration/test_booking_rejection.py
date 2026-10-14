"""A business refusing direct ``POST /bookings``: classified, retryable only when safe."""
from __future__ import annotations

import httpx
import pytest

from booking_agent.rest_client import BookingRequestError
from booking_agent_cli.commands.flow import AGENT_PROFILE_UNRESOLVABLE_USER_MESSAGE
from booking_agent_cli.session import must_load, save_session
from support.fixtures.services import set_zero_price_free_service
from support.mock.link_cli import YUMA_SHIPPING_ADDRESS, mock_link_cli_user_info
from integration.test_flow_gates import (
    _BUYER,
    _SERVICE_ID,
    _YUMA,
    _business_use,
    _geocoder,
    _init,
    _invoke,
    _select_first_and_resolve,
    _stdout_json,
)

_RETRY = f"booking-agent flow book --service-id {_SERVICE_ID}"


@pytest.fixture
def direct_booking(runner, isolated_session, monkeypatch):
    """A free service at BUYER_RESOLVED, so ``flow book`` takes the direct path."""
    import booking_agent_cli.commands.flow as flow

    set_zero_price_free_service(monkeypatch, _SERVICE_ID)
    mock_link_cli_user_info(monkeypatch, _BUYER, shipping_addresses=[YUMA_SHIPPING_ADDRESS])
    _geocoder(monkeypatch, _YUMA)
    _init(runner)
    assert _business_use(runner).exit_code == 0
    assert _invoke(runner, "flow", "calendar", "skip").exit_code == 0
    assert _invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID).exit_code == 0
    _select_first_and_resolve(runner, _stdout_json(_invoke(runner, "flow", "slots", "list")))

    backend = flow.resolve_backend()
    original = backend.create_booking
    keys: list = []
    outcomes: list = []

    async def create_booking(*args, idempotency_key=None, **kwargs):
        keys.append(idempotency_key)
        outcome = outcomes.pop(0) if outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return await original(*args, idempotency_key=idempotency_key, **kwargs)

    monkeypatch.setattr(backend, "create_booking", create_booking)
    return keys, outcomes


def _book(runner) -> tuple[int, dict]:
    r = _invoke(runner, "flow", "book", "--service-id", _SERVICE_ID)
    return r.exit_code, _stdout_json(r)


def _profile_refused() -> BookingRequestError:
    return BookingRequestError(400, "invalid_profile_url")


def test_first_profile_refusal_asks_for_a_retry_without_a_support_offer(runner, direct_booking):
    keys, outcomes = direct_booking
    outcomes.append(_profile_refused())

    code, out = _book(runner)

    assert code == 1
    assert out["data"]["error"]["code"] == "agent_profile_unresolvable"
    assert out["next_command"] == _RETRY
    assert out["user_message"] == AGENT_PROFILE_UNRESOLVABLE_USER_MESSAGE
    assert out["data"]["business_error"] == {
        "http_status": 400, "business_code": "invalid_profile_url",
    }
    assert out.get("support_report_offered") is None
    pending = must_load().pending_booking_attempt
    assert pending["idempotency_key"] == keys[0]
    assert pending["retry_command"] == _RETRY
    assert _stdout_json(_invoke(runner, "flow", "status"))["next_command"] == _RETRY


def test_retry_resends_the_same_key_and_clears_it_on_success(runner, direct_booking):
    keys, outcomes = direct_booking
    outcomes.append(_profile_refused())
    _book(runner)

    code, out = _book(runner)

    assert code == 0, out
    assert keys[0] and keys == [keys[0], keys[0]]
    session = must_load()
    assert session.flow_phase == "BOOKED"
    assert session.pending_booking_attempt is None


def test_a_repeated_profile_refusal_offers_a_support_report(runner, direct_booking):
    keys, outcomes = direct_booking
    outcomes.extend([_profile_refused(), _profile_refused()])
    _book(runner)

    code, out = _book(runner)

    assert code == 1
    assert out["data"]["error"]["code"] == "agent_profile_unresolvable"
    assert out.get("support_report_offered") is True
    assert keys == [keys[0], keys[0]]


def test_a_changed_booking_gets_a_new_key(runner, direct_booking):
    keys, outcomes = direct_booking
    outcomes.append(_profile_refused())
    _book(runner)
    session = must_load()
    session.booking_notes = "please call on arrival"
    save_session(session)

    code, _out = _book(runner)

    assert code == 0
    assert len(keys) == 2 and keys[0] != keys[1]


def test_another_business_code_stays_create_booking_failed_without_a_stored_key(
    runner, direct_booking,
):
    _keys, outcomes = direct_booking
    outcomes.append(BookingRequestError(409, "slot_unavailable"))

    code, out = _book(runner)

    assert code == 1
    assert out["data"]["error"]["code"] == "create_booking_failed"
    assert "HTTP 409" in out["data"]["error"]["message"]
    assert "slot_unavailable" in out["data"]["error"]["message"]
    assert out["data"]["business_error"] == {
        "http_status": 409, "business_code": "slot_unavailable",
    }
    assert must_load().pending_booking_attempt is None


def test_a_transport_failure_stays_generic(runner, direct_booking):
    _keys, outcomes = direct_booking
    outcomes.append(httpx.ConnectError("refused"))

    code, out = _book(runner)

    assert code == 1
    assert out["data"]["error"]["code"] == "create_booking_failed"
    assert "business_error" not in out["data"]
    assert must_load().pending_booking_attempt is None
