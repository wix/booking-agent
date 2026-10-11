"""Resolve configured USP registry URL to the HTTP POST target for service search.

``USP_REGISTRY_URL`` may be:
- A vendor-specific full POST URL (e.g. interim Wix semantic-search ``.../v1/search``).
- A registry API base URL where the spec path ``POST /registry/search_services`` applies.
"""
from __future__ import annotations

from urllib.parse import urlparse


DEFAULT_REGISTRY_URL = (
    "https://www.wixapis.com/_api/usp-registry/registry/search_services"
)


def registry_search_post_url(configured_url: str) -> str:
    """Return the URL to POST the ``search_services`` body to.

    If *configured_url* already points at a known non-spec search endpoint, it is
    returned unchanged so existing deployments keep working.
    """
    raw = (configured_url or "").strip()
    if not raw:
        return raw
    lower = raw.lower()
    # Spec-shaped path already
    if "search_services" in lower:
        return raw
    # Interim Wix semantic-search and similar ``.../search`` POST endpoints
    if "semantic-search" in lower or lower.rstrip("/").endswith("/search"):
        return raw
    # Treat as registry HTTP origin + spec path
    base = raw.rstrip("/")
    return f"{base}/registry/search_services"
