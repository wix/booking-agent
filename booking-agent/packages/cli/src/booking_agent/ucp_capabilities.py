"""Capability-agnostic seam for binding UCP negotiation to checkout (GH-123).

UCP resolves capabilities in a three-party negotiation: the platform advertises
support in its ``/.well-known/ucp`` profile, the business intersects that with
its own ``business_schema`` **and cart context**, and returns the resolved set in
the checkout **response** (``checkout.ucp.capabilities`` / ``checkout.ucp.version``).

Per UCP the response is runtime-authoritative:

    "Platforms MUST treat the available_instruments in the response as
    authoritative for that checkout ... and MUST NOT attempt to use instrument
    types or apply constraints that contradict it."

The same rule applies to the capability set. This module therefore:

* treats the platform-side :func:`booking_agent.ucp_negotiation.intersect_ucp_capabilities`
  result as a **pre-flight discovery gate only** (what the platform *supports*),
  and
* validates the business's **response echo** with a defer-to-response rule -
  the platform accepts any set the business resolves as long as it does not
  activate a capability/version the platform cannot support.

The registry keeps this extensible: new capabilities (e.g. AP2 agentic payments)
register a handler under their namespace with no changes to the checkout flow.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from .ucp_negotiation import _capabilities_from_doc
from .namespace import CAP_PAY_AT_SERVICE
from .ucp_profile import CAP_PAID_BOOKINGS, UcpProfileValidationError


def capabilities_from_checkout_response(checkout: Dict[str, Any]) -> Dict[str, List[str]]:
    """Extract the authoritative ``{namespace: [versions]}`` echo from a checkout response.

    Accepts both the profile-style ``{cap: [{"version": ...}]}`` shape and the
    flattened ``{cap: ["<version>", ...]}`` shape businesses may echo.
    """
    ucp = checkout.get("ucp") if isinstance(checkout, dict) else None
    if not isinstance(ucp, dict):
        return {}
    caps = ucp.get("capabilities")
    if not isinstance(caps, dict):
        return {}
    # Detect flattened form (values are lists of strings) vs profile form.
    flattened: Dict[str, List[str]] = {}
    profile_form = False
    for cap_id, entries in caps.items():
        if isinstance(entries, list) and all(isinstance(e, str) for e in entries):
            flattened[cap_id] = [e for e in entries if e]
        else:
            profile_form = True
            break
    if profile_form:
        # Reuse the profile parser (expects {"ucp": {"capabilities": ...}}).
        return _capabilities_from_doc({"ucp": {"capabilities": caps}})
    return flattened


def version_from_checkout_response(checkout: Dict[str, Any]) -> str | None:
    ucp = checkout.get("ucp") if isinstance(checkout, dict) else None
    if isinstance(ucp, dict) and isinstance(ucp.get("version"), str) and ucp["version"]:
        return ucp["version"]
    return None


# A handler validates one capability namespace's response echo against what the
# platform supports for that namespace. It MUST raise UcpProfileValidationError
# when the response activates a version the platform cannot support, and MUST NOT
# object to the business narrowing the set (defer-to-response).
CapabilityHandler = Callable[[List[str], List[str]], None]


def _validate_versions_supported(
    namespace: str, echoed_versions: List[str], platform_supported: List[str]
) -> None:
    if not platform_supported:
        # Platform declares the namespace with no version constraint - accept.
        return
    unsupported = [v for v in echoed_versions if v not in set(platform_supported)]
    if unsupported:
        raise UcpProfileValidationError(
            f"UCP checkout response activated {namespace!r} version(s) "
            f"{unsupported!r} the platform does not support "
            f"(supported: {sorted(platform_supported)!r}). Cannot proceed with checkout."
        )


def _paid_bookings_handler(echoed_versions: List[str], platform_supported: List[str]) -> None:
    _validate_versions_supported(CAP_PAID_BOOKINGS, echoed_versions, platform_supported)


def _pay_at_service_handler(echoed_versions: List[str], platform_supported: List[str]) -> None:
    _validate_versions_supported(CAP_PAY_AT_SERVICE, echoed_versions, platform_supported)


# namespace -> handler. Register new capabilities (e.g. AP2) here.
CAPABILITY_HANDLERS: Dict[str, CapabilityHandler] = {
    CAP_PAID_BOOKINGS: _paid_bookings_handler,
    CAP_PAY_AT_SERVICE: _pay_at_service_handler,
}


def validate_checkout_capabilities(
    checkout: Dict[str, Any],
    platform_supported: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Validate a checkout response's capability echo and return the authoritative set.

    ``platform_supported`` is the platform-side pre-flight intersection
    (``intersect_ucp_capabilities``) - i.e. what the platform can do. The
    returned mapping is the **response echo**, which callers should persist as
    the authoritative negotiated set.

    Rules (defer-to-response):
    * The business MAY narrow the set (echo ⊆ platform_supported) - accepted.
    * If the echo activates a namespace/version the platform does not support,
      raise :class:`UcpProfileValidationError` (the platform cannot proceed).
    * All offending namespaces are named in that one error so a static merchant
      map cannot hide later activations behind the first.
    * Registered per-namespace handlers run their own checks; unregistered
      namespaces fall back to a generic version-support check so unknown
      activations still cannot slip through unsupported.
    """
    echo = capabilities_from_checkout_response(checkout)
    problems: List[str] = []
    for namespace, echoed_versions in echo.items():
        # A namespace the platform did not negotiate at all cannot be activated,
        # regardless of whether a handler is registered for it.
        if namespace not in platform_supported:
            problems.append(
                f"unsupported capability {namespace!r} "
                f"(activated {echoed_versions!r}; platform supports "
                f"{sorted(platform_supported.keys())!r})"
            )
            continue
        supported = platform_supported.get(namespace, [])
        handler = CAPABILITY_HANDLERS.get(namespace)
        try:
            if handler is not None:
                handler(echoed_versions, supported)
            else:
                _validate_versions_supported(namespace, echoed_versions, supported)
        except UcpProfileValidationError as exc:
            problems.append(str(exc).rstrip("."))
    if problems:
        raise UcpProfileValidationError(
            "UCP checkout response activated unsupported capability set: "
            + "; ".join(problems)
            + ". Cannot proceed with checkout."
        )
    return echo
