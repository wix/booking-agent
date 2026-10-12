"""Checkout-mode prerequisite checks and mode selection helpers."""
from __future__ import annotations

from typing import List, Optional, Tuple

from booking_agent.platform_profile import platform_profile_doc, platform_profile_url
from booking_agent.ucp_profile import UcpNativeContext



class PrereqFailure(Exception):
    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def supported_checkout_modes_from_profile(
    uctx: Optional[UcpNativeContext],
) -> Tuple[List[str], str]:
    """Return (supported_modes, checkout_mode_reason) after profile resolution."""
    if uctx is not None:
        rest_base = uctx.ucp_rest_endpoint()
        return (
            ["ucp_native"],
            f"UCP REST bindings detected at {rest_base}",
        )
    return (
        [],
        "This business does not support UCP-native checkout, which v2 requires.",
    )


def select_checkout_mode(supported_modes: List[str]) -> str:
    """v2 supports UCP-native checkout only."""
    if "ucp_native" in supported_modes:
        return "ucp_native"
    raise PrereqFailure(
        code="ucp_native_required",
        message=(
            "v2 requires UCP-native checkout. This business cannot be booked "
            "through the agent."
        ),
    )


def run_prereq_check_ucp() -> None:
    """Validate UCP-native checkout prerequisites.

    Validate the packaged source-of-truth document and its configured identity
    URL. The actual document is fetched during checkout negotiation.
    """
    doc = platform_profile_doc()
    ucp = doc.get("ucp")
    if not isinstance(ucp, dict) or not ucp.get("version"):
        raise PrereqFailure(
            code="ucp_platform_profile_invalid",
            message="UCP platform profile source of truth is missing ucp.version.",
        )
    if not platform_profile_url():
        raise PrereqFailure(
            code="ucp_platform_profile_invalid",
            message="UCP platform profile URL is not configured.",
        )
