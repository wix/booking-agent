"""Every privileged client method must reach the wire authorized.

This is the structural guard.  Spec issue #9's own checklist drifted to "all 13
privileged_scoped methods" after registry writes were re-tiered to the platform
tier, which is exactly how a method ships unauthenticated: a list maintained by
hand stops matching the code.  Here the list is data (``usp_methods``) and this
test walks it, so adding a client method without registering its tier fails.
"""
from __future__ import annotations

import inspect

import pytest

from booking_agent import usp_methods
from booking_agent.authorization import BookingScopedCredential
from booking_agent.pop import PopKey
from booking_agent.rest_client import USPBusinessRestClient
from support.mock.pop_transport import MockPopBusinessTransport, business_profile

# Arguments sufficient to drive each client method once.
CALL_ARGS = {
    "list_services": ((), {}),
    "get_service": (("svc_1",), {}),
    "query_availability": (("svc_1", "2026-09-01", "2026-09-02"), {}),
    "hold_slot": (("slot_1", "svc_1"), {}),
    "release_hold": (("hold_1",), {}),
    "create_booking": ((), {"service_id": "svc_1", "slot_id": "slot_1",
                            "buyer": {"name": "A", "email": "a@b.c"}}),
    "get_booking": (("bk_1",), {}),
    "update_booking": (("bk_1",), {"notes": "x"}),
    "confirm_booking": (("bk_1",), {}),
    "cancel_booking": (("bk_1",), {}),
    "reschedule_booking": (("bk_1", "slot_2"), {}),
    "confirm_payment": (("bk_1",), {}),
}


def test_every_client_method_is_registered_with_an_access_tier():
    for usp_method, attribute in usp_methods.CLIENT_METHODS.items():
        assert hasattr(USPBusinessRestClient, attribute), (
            f"{usp_method} maps to missing client attribute {attribute!r}"
        )
        # Raises if the method has no registered tier.
        usp_methods.access_tier(usp_method)


def test_scoped_methods_accept_a_credential():
    """A privileged_scoped method that cannot be given a credential cannot be
    authorized, however carefully the caller behaves."""
    for usp_method, attribute in usp_methods.CLIENT_METHODS.items():
        if usp_methods.access_tier(usp_method) != usp_methods.PRIVILEGED_SCOPED:
            continue
        signature = inspect.signature(getattr(USPBusinessRestClient, attribute))
        assert "credential" in signature.parameters, (
            f"{attribute} is privileged_scoped but takes no credential"
        )


@pytest.mark.parametrize(
    "usp_method,attribute",
    sorted(usp_methods.CLIENT_METHODS.items()),
)
@pytest.mark.asyncio
async def test_authorization_matches_the_declared_tier(monkeypatch, usp_method, attribute):
    """Public calls carry no proof; privileged calls always do."""
    key = PopKey.generate()
    mock = MockPopBusinessTransport().patch(monkeypatch)
    client = USPBusinessRestClient(
        business_profile(), agent_profile_url="https://agent.example/p.json",
        pop_key=key, authenticate=True,
    )
    tier = usp_methods.access_tier(usp_method)

    args, kwargs = CALL_ARGS[attribute]
    if tier == usp_methods.PRIVILEGED_SCOPED:
        kwargs = dict(kwargs)
        kwargs["credential"] = BookingScopedCredential.model_validate({
            "value": "bsc_enumeration_probe_credential",
            "expires_at": "2099-01-01T00:00:00Z",
            "cnf": {"jkt": key.jkt},
        })
    if attribute == "confirm_payment":
        from booking_agent.models.wire import USPPaymentResult
        args = (*args, USPPaymentResult(
            status="succeeded",
            transaction_id="txn_1",
            amount_paid=100,
            currency="USD",
        ))

    try:
        await getattr(client, attribute)(*args, **kwargs)
    except Exception:
        # The mock rejects most of these; we only care what went on the wire.
        pass

    assert mock.requests, f"{attribute} issued no request"
    request = mock.requests[-1]
    if tier == usp_methods.PUBLIC:
        assert "DPoP" not in request.headers, (
            f"{usp_method} is public but sent a proof, binding our key to a "
            f"business we are only browsing"
        )
    else:
        assert "DPoP" in request.headers, (
            f"{usp_method} is {tier} but reached the wire with no proof"
        )
    if tier == usp_methods.PRIVILEGED_SCOPED:
        assert request.headers.get("Authorization", "").startswith("DPoP "), (
            f"{usp_method} is privileged_scoped but presented no credential"
        )
    if tier == usp_methods.PRIVILEGED_PLATFORM:
        assert "Authorization" not in request.headers, (
            f"{usp_method} is an issuance call and must send no Authorization header"
        )


def test_unimplemented_methods_are_recorded_not_forgotten():
    """The client covers 7 of the 11 scoped methods; the gap is explicit.

    Waitlist operations have no client method at all, so there is nothing to
    authorize yet.  Tracked upstream rather than silently absent.
    """
    assert usp_methods.UNIMPLEMENTED, "the registry claims full coverage; verify that"
    missing_scoped = usp_methods.PRIVILEGED_SCOPED_METHODS & usp_methods.UNIMPLEMENTED
    assert missing_scoped == {
        "usp_waitlist_get",
        "usp_waitlist_leave",
        "usp_waitlist_accept",
        "usp_waitlist_decline",
    }
    # Registry writes are platform-tier, not scoped -- they were re-tiered in
    # USP 2026-08-14.  Guarded here because the old tiering is still repeated in
    # older comments on spec issue #9.
    for method in ("usp_registry_update", "usp_registry_delete", "usp_registry_register"):
        assert usp_methods.access_tier(method) == usp_methods.PRIVILEGED_PLATFORM
