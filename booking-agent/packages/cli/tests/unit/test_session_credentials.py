"""Credential persistence, endpoint scoping, and safe key retirement."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from booking_agent.authorization import BookingScopedCredential
from booking_agent.pop import PopKey
import booking_agent_cli.session as session_module
from booking_agent_cli.session import (
    BookingRef,
    Business,
    Session,
    SessionStore,
    clear_credential,
    credential_for,
    endpoint_still_in_use,
    record_issued_credential,
)

ENDPOINT = "https://biz.example/usp/v1"
CHECKOUT_ENDPOINT = "https://checkout.biz.example/ucp/v1"
OTHER = "https://other.example/usp/v1"


def _credential(key: PopKey, *, hours: int = 24) -> BookingScopedCredential:
    return BookingScopedCredential.model_validate({
        "value": "bsc_persisted_credential_value_x",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(),
        "cnf": {"jkt": key.jkt},
    })


def _session(**booking) -> Session:
    return Session(booking=BookingRef(**booking))


def test_credential_round_trips_through_the_session():
    key = PopKey.generate()
    session = _session(id="bk_1")
    record_issued_credential(
        session, _credential(key),
        endpoint=ENDPOINT,
        booking_url="https://biz.example/b/1",
        revision="rev-1",
    )

    restored = credential_for(session, endpoint=ENDPOINT)
    assert restored is not None
    assert restored.value == "bsc_persisted_credential_value_x"
    assert restored.cnf.jkt == key.jkt
    assert session.booking.booking_url == "https://biz.example/b/1"
    assert session.booking.revision == "rev-1"


def test_credential_is_never_returned_for_a_different_business():
    """A credential is a secret scoped to one resource at one business.

    Handing it to another endpoint would leak it to a party with no claim to it,
    so the mismatch returns nothing rather than the credential.
    """
    key = PopKey.generate()
    session = _session(id="bk_1")
    record_issued_credential(session, _credential(key), endpoint=ENDPOINT)

    assert credential_for(session, endpoint=OTHER) is None
    assert credential_for(session, endpoint=ENDPOINT) is not None


def test_checkout_credential_is_available_to_same_session_usp_binding():
    key = PopKey.generate()
    session = Session(
        business=Business(rest_endpoint=ENDPOINT),
        booking=BookingRef(id="bk_1"),
        ucp_checkout_rest_base=CHECKOUT_ENDPOINT,
    )
    record_issued_credential(
        session,
        _credential(key),
        endpoint=CHECKOUT_ENDPOINT,
    )

    restored = session_module.credential_for_session_resource(
        session,
        endpoint=ENDPOINT,
    )

    assert restored is not None
    assert restored.value == "bsc_persisted_credential_value_x"


def test_checkout_credential_is_never_returned_for_unrelated_endpoint():
    key = PopKey.generate()
    session = Session(
        business=Business(rest_endpoint=ENDPOINT),
        booking=BookingRef(id="bk_1"),
        ucp_checkout_rest_base=CHECKOUT_ENDPOINT,
    )
    record_issued_credential(
        session,
        _credential(key),
        endpoint=CHECKOUT_ENDPOINT,
    )

    assert session_module.credential_for_session_resource(
        session,
        endpoint=OTHER,
    ) is None


def test_clear_credential_removes_every_part_of_it():
    key = PopKey.generate()
    session = _session(id="bk_1")
    record_issued_credential(session, _credential(key), endpoint=ENDPOINT)
    clear_credential(session)

    assert credential_for(session, endpoint=ENDPOINT) is None
    assert session.booking.credential_jkt is None
    assert session.booking.credential_expires_at is None
    # The endpoint and booking_url survive: they are not secrets and the handoff
    # path still needs them.
    assert session.booking.endpoint == ENDPOINT


# -- The refcount that keeps other bookings cancellable --------------------

def _store_with(*sessions: Session) -> SessionStore:
    return SessionStore(sessions=list(sessions))


def test_endpoint_is_in_use_while_another_session_holds_a_live_credential():
    key = PopKey.generate()
    first, second = _session(id="a"), _session(id="b")
    first.id, second.id = "sess-a", "sess-b"
    record_issued_credential(first, _credential(key), endpoint=ENDPOINT)
    record_issued_credential(second, _credential(key), endpoint=ENDPOINT)
    store = _store_with(first, second)

    # Cancelling the first booking must NOT free the key: the second booking's
    # credential is bound to it, and USP defines no re-issuance path, so the
    # second booking would become permanently uncancellable.
    assert endpoint_still_in_use(store, ENDPOINT, excluding_session_id="sess-a") is True


def test_endpoint_is_free_once_the_last_credential_is_gone():
    key = PopKey.generate()
    only = _session()
    only.id = "sess-a"
    record_issued_credential(only, _credential(key), endpoint=ENDPOINT)
    store = _store_with(only)

    assert endpoint_still_in_use(store, ENDPOINT, excluding_session_id="sess-a") is False


def test_expired_credentials_do_not_keep_a_key_alive():
    key = PopKey.generate()
    stale, current = _session(), _session()
    stale.id, current.id = "sess-stale", "sess-current"
    record_issued_credential(stale, _credential(key, hours=-1), endpoint=ENDPOINT)
    store = _store_with(stale, current)

    assert endpoint_still_in_use(store, ENDPOINT, excluding_session_id="sess-current") is False


def test_credentials_at_other_endpoints_are_irrelevant():
    key = PopKey.generate()
    elsewhere = _session()
    elsewhere.id = "sess-elsewhere"
    record_issued_credential(elsewhere, _credential(key), endpoint=OTHER)
    store = _store_with(elsewhere)

    assert endpoint_still_in_use(store, ENDPOINT) is False


def test_booking_ref_survives_a_session_file_without_the_new_fields():
    """Sessions written before this change must still load."""
    from booking_agent_cli.session import _session_from_dict

    session = _session_from_dict({
        "id": "sess-old",
        "booking": {"id": "bk_1", "checkout_path": "redirect"},
    })
    assert session.booking.id == "bk_1"
    assert session.booking.credential is None
    assert session.booking.endpoint is None
