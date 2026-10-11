"""The USP method access tiers (USP Section 9.2.2), as data.

This exists so that "which operations must carry authorization" is a fact the
code can iterate over, not a list a human keeps in sync.  The equivalent list on
spec issue #9 drifted to "all 13 privileged_scoped methods" after registry
writes were re-tiered to ``privileged_platform``, which is exactly the failure
mode a hand-maintained enumeration invites.

``tests/test_scoped_method_authorization.py`` walks ``CLIENT_METHODS`` and
asserts every privileged entry reaches the wire with a proof and every public
entry reaches it without one.  Adding a client method without registering it
here fails that test.
"""
from __future__ import annotations

from typing import Dict, FrozenSet

PUBLIC = "public"
PRIVILEGED_PLATFORM = "privileged_platform"
PRIVILEGED_SCOPED = "privileged_scoped"

#: Authentication optional; these stay anonymous.
PUBLIC_METHODS: FrozenSet[str] = frozenset({
    "usp_services_list",
    "usp_services_get",
    "usp_services_lookup",
    "usp_services_feed",
    "usp_availability_query",
    "usp_registry_search_business",
    "usp_registry_search_services",
    "usp_registry_get",
})

#: Authenticated as the platform.  These are the *issuance* methods: they carry a
#: proof and no credential, and the resource-creating ones receive a
#: ``booking_scoped_credential`` bound to the proof key in the response.
#:
#: Registry writes live here, not in the scoped set: a registry entry has exactly
#: one owner, so it is authorized at platform tier against the registering
#: platform's bound ``jkt`` rather than needing a second credential type.
PRIVILEGED_PLATFORM_METHODS: FrozenSet[str] = frozenset({
    "usp_services_feed_subscribe",
    "usp_availability_hold",
    "usp_bookings_create",
    "usp_waitlist_join",
    "usp_waitlist_list",
    "usp_registry_register",
    "usp_registry_update",
    "usp_registry_delete",
})

#: Authorized per-resource: these carry a credential *and* a proof over it.
#: Eleven methods, not thirteen -- the registry pair moved to the platform tier.
PRIVILEGED_SCOPED_METHODS: FrozenSet[str] = frozenset({
    "usp_availability_release",
    "usp_bookings_get",
    "usp_bookings_update",
    "usp_bookings_confirm",
    "usp_bookings_cancel",
    "usp_bookings_reschedule",
    "usp_bookings_confirm_payment",
    "usp_waitlist_get",
    "usp_waitlist_leave",
    "usp_waitlist_accept",
    "usp_waitlist_decline",
})

ACCESS_BY_METHOD: Dict[str, str] = {
    **{m: PUBLIC for m in PUBLIC_METHODS},
    **{m: PRIVILEGED_PLATFORM for m in PRIVILEGED_PLATFORM_METHODS},
    **{m: PRIVILEGED_SCOPED for m in PRIVILEGED_SCOPED_METHODS},
}

#: USP method name -> ``USPBusinessRestClient`` attribute implementing it.
#:
#: Only the methods this client actually has.  Everything in ``UNIMPLEMENTED``
#: below is absent from the client entirely, so there is nothing to authorize
#: yet; that gap is tracked upstream rather than papered over here.
CLIENT_METHODS: Dict[str, str] = {
    "usp_services_list": "list_services",
    "usp_services_get": "get_service",
    "usp_availability_query": "query_availability",
    "usp_availability_hold": "hold_slot",
    "usp_availability_release": "release_hold",
    "usp_bookings_create": "create_booking",
    "usp_bookings_get": "get_booking",
    "usp_bookings_update": "update_booking",
    "usp_bookings_confirm": "confirm_booking",
    "usp_bookings_cancel": "cancel_booking",
    "usp_bookings_reschedule": "reschedule_booking",
    "usp_bookings_confirm_payment": "confirm_payment",
}

#: Specified by USP and reachable over REST, but not implemented in this client.
#: Tracked upstream -- see the follow-up issue linked from spec issue #9.  Listed
#: explicitly so the coverage gap is visible rather than implied by absence.
UNIMPLEMENTED: FrozenSet[str] = frozenset(
    set(ACCESS_BY_METHOD) - set(CLIENT_METHODS)
)


def access_tier(method: str) -> str:
    """Return the access tier for *method*, raising on an unregistered name."""
    try:
        return ACCESS_BY_METHOD[method]
    except KeyError:
        raise KeyError(
            f"{method!r} is not a registered USP method; add it to usp_methods.py "
            f"with its access tier before wiring it up."
        ) from None
