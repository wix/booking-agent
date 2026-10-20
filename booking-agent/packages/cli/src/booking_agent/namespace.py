"""USP protocol namespace authority (specification Section 2.5).

Capability keys are ``dev.usp-protocol.*``. ``spec`` and ``schema`` URLs for
those keys MUST use origin ``https://usp-protocol.dev``.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

AUTHORITY_HOST = "usp-protocol.dev"
AUTHORITY_ORIGIN = f"https://{AUTHORITY_HOST}"

CAPABILITY_PREFIX = "dev.usp-protocol."
SVC_SERVICES = "dev.usp-protocol.services"
CAP_CATALOG = "dev.usp-protocol.services.catalog"
CAP_AVAILABILITY = "dev.usp-protocol.services.availability"
CAP_BOOKINGS = "dev.usp-protocol.services.bookings"
CAP_PAID_BOOKINGS = "dev.usp-protocol.services.paid_bookings"
CAP_PAY_AT_SERVICE = "dev.usp-protocol.services.pay_at_service"


class NamespaceOriginError(ValueError):
    """Raised when a ``dev.usp-protocol.*`` key is bound to a non-authority origin."""


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def requires_authority_origin(key: str) -> bool:
    return key == SVC_SERVICES or key.startswith(CAPABILITY_PREFIX)


def check_origin_binding(
    key: str,
    *,
    spec: Optional[str] = None,
    schema: Optional[str] = None,
) -> None:
    """Enforce Section 2.5 origin binding when *spec* or *schema* is present."""
    if not requires_authority_origin(key):
        return
    for label, url in (("spec", spec), ("schema", schema)):
        if not url:
            continue
        origin = _origin(url)
        if origin != AUTHORITY_ORIGIN:
            raise NamespaceOriginError(
                f"{key} {label} origin must be {AUTHORITY_ORIGIN!r}, got {url!r}"
            )
