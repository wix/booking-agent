"""UCP platform vs business profile version + capability intersection (GH-064, GH-058 subset, GH-095)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .ucp_profile import UcpProfileValidationError


def _protocol_versions(doc: Dict[str, Any]) -> List[str]:
    ucp = doc.get("ucp") or doc
    if not isinstance(ucp, dict):
        return []
    versions: List[str] = []
    v = ucp.get("version")
    if isinstance(v, str) and v:
        versions.append(v)
    extra = ucp.get("supported_versions")
    if isinstance(extra, list):
        versions.extend([x for x in extra if isinstance(x, str) and x])
    caps = ucp.get("capabilities") or {}
    if isinstance(caps, dict):
        for entry in caps.get("dev.ucp.shopping.checkout") or []:
            if isinstance(entry, dict) and isinstance(entry.get("version"), str):
                versions.append(entry["version"])
    seen = []
    for item in versions:
        if item not in seen:
            seen.append(item)
    return seen


def highest_common_version(a: List[str], b: List[str]) -> Optional[str]:
    """Pick the highest version string present in both lists (lexicographic fallback)."""
    sa, sb = set(a), set(b)
    common = sa & sb
    if not common:
        return None
    return sorted(common)[-1]


def negotiate_ucp_protocol_version(
    platform_doc: Dict[str, Any], business_doc: Dict[str, Any],
) -> str:
    """Return mutually supported UCP protocol version or raise."""
    pv = _protocol_versions(platform_doc)
    bv = _protocol_versions(business_doc)
    if not bv:
        raise UcpProfileValidationError("Business profile must declare ucp.version")
    if not pv:
        return bv[0]
    pick = highest_common_version(pv, bv)
    if not pick:
        raise UcpProfileValidationError(
            f"No overlapping UCP protocol version between platform {pv!r} and business {bv!r}"
        )
    return pick


def _capabilities_from_doc(doc: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return {capability_id: [versions...]} from a profile document."""
    ucp = doc.get("ucp") or doc
    if not isinstance(ucp, dict):
        return {}
    caps_raw = ucp.get("capabilities") or {}
    if not isinstance(caps_raw, dict):
        return {}
    result: Dict[str, List[str]] = {}
    for cap_id, entries in caps_raw.items():
        if not isinstance(entries, list):
            continue
        versions: List[str] = [
            e["version"]
            for e in entries
            if isinstance(e, dict) and isinstance(e.get("version"), str) and e["version"]
        ]
        result[cap_id] = versions
    return result


def _extends_map_from_doc(doc: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return {capability_id: [parent_capability_id, ...]} for extends declarations."""
    ucp = doc.get("ucp") or doc
    if not isinstance(ucp, dict):
        return {}
    caps_raw = ucp.get("capabilities") or {}
    result: Dict[str, List[str]] = {}
    for cap_id, entries in caps_raw.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            raw = e.get("extends")
            parents: List[str] = []
            if isinstance(raw, str) and raw:
                parents = [raw]
            elif isinstance(raw, list):
                parents = [p for p in raw if isinstance(p, str) and p]
            if parents:
                result[cap_id] = parents
                break
    return result


def intersect_ucp_capabilities(
    platform_doc: Dict[str, Any],
    business_doc: Dict[str, Any],
) -> Dict[str, List[str]]:
    """Intersect platform and business capabilities per UCP negotiation rules (GH-095).

    Algorithm:
    1. Build per-capability version lists for both sides.
    2. Keep only capabilities the platform declares; within those, keep only
       the version strings that appear in both lists.
    3. Prune business capabilities whose ``extends`` parent was itself pruned.
    4. Raise :class:`UcpProfileValidationError` with an actionable message when
       the intersection is empty.

    When the platform profile carries no ``capabilities`` block at all the
    platform is treated as unconstrained and the full business capability set
    is returned.

    Returns the pruned ``{capability_id: [version, ...]}`` mapping.
    """
    plat_caps = _capabilities_from_doc(platform_doc)
    bus_caps = _capabilities_from_doc(business_doc)
    bus_extends = _extends_map_from_doc(business_doc)

    # Platform fixture without a capabilities block - treat as unconstrained.
    if not plat_caps:
        return bus_caps

    # Step 1: keep capabilities the platform supports; intersect version lists.
    intersection: Dict[str, List[str]] = {}
    for cap_id, bus_versions in bus_caps.items():
        if cap_id not in plat_caps:
            continue
        plat_versions = plat_caps[cap_id]
        if plat_versions:
            common = [v for v in bus_versions if v in set(plat_versions)]
        else:
            # Platform declares the capability but with no version constraint.
            common = list(bus_versions)
        if common:
            intersection[cap_id] = common

    # Step 2: prune extensions whose parent capability was pruned.
    pruned: Dict[str, List[str]] = {}
    for cap_id, versions in intersection.items():
        parents = bus_extends.get(cap_id) or []
        if parents and any(parent not in intersection for parent in parents):
            continue
        pruned[cap_id] = versions

    if not pruned:
        raise UcpProfileValidationError(
            f"UCP capability intersection is empty: "
            f"platform supports {sorted(plat_caps)!r}, "
            f"business requires {sorted(bus_caps)!r}. "
            "Cannot proceed with checkout."
        )

    return pruned


async def load_platform_profile(url: str) -> Dict[str, Any]:
    """Fetch the asserted platform identity for version/capability negotiation."""
    return await load_platform_profile_json(url)


async def load_platform_profile_json(url: str) -> Dict[str, Any]:
    from booking_agent.http_retry import with_http_retry
    from booking_agent.http_trace import traced_async_client

    async def _get():
        async with traced_async_client(timeout=30.0, follow_redirects=True) as client:
            return await client.get(url)

    r = await with_http_retry(_get)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise UcpProfileValidationError("Platform profile must be a JSON object")
    return data
