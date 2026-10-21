"""Schema of the consented support note, and the check the CLI runs before storing
or sending one.

The support ingest checks every note against ``SUPPORT_BUNDLE_SCHEMA`` with the same
JSON Schema subset that ``validate_bundle`` implements, so a note the ingest would
reject never leaves the machine.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List

SUPPORT_BUNDLE_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://booking-agent.dev/schemas/support-bundle-v1.json",
    "title": "Booking Agent consented support report, bundle_version 1",
    "description": "Every value is projected from an allowlist. Unknown keys are rejected at every level.",
    "type": "object",
    "additionalProperties": False,
    "required": ["bundle_version", "created_at", "env", "error", "build_errors"],
    "properties": {
        "bundle_version": {"const": 1},
        "created_at": {"$ref": "#/$defs/ts_required"},
        "correlation_id": {"$ref": "#/$defs/id"},
        "env": {
            "type": "object",
            "additionalProperties": False,
            "required": ["cli_version"],
            "properties": {
                "skill_version": {"$ref": "#/$defs/id"},
                "cli_version": {"$ref": "#/$defs/id"},
                "cli_build": {"type": ["string", "null"], "pattern": "^[0-9a-f]{7,64}$"},
                "core_version": {"$ref": "#/$defs/id"},
                "python": {"$ref": "#/$defs/id"},
                "os": {"$ref": "#/$defs/label"},
                "machine": {"$ref": "#/$defs/id"},
            },
        },
        "error": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code"],
            "properties": {
                "code": {"$ref": "#/$defs/id"},
                "phase": {"$ref": "#/$defs/id"},
                "occurrences": {"type": "integer", "minimum": 1},
                "offer_reason": {
                    "enum": ["unknown_code", "internal_error", "repeated", "captured_no_next", None],
                },
                "next_command_before_offer": {"$ref": "#/$defs/command"},
            },
        },
        "exception": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"$ref": "#/$defs/qualname"},
                        "chain": {"type": "array", "maxItems": 5, "items": {"$ref": "#/$defs/qualname"}},
                        "frames": {
                            "type": "array",
                            "maxItems": 40,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "file": {
                                        "type": "string",
                                        "maxLength": 300,
                                        "pattern": "^[^\\x00-\\x1f\\x7f]*$",
                                    },
                                    "line": {"type": ["integer", "null"]},
                                    "function": {"$ref": "#/$defs/qualname"},
                                    "code": {"type": ["string", "null"], "maxLength": 200},
                                },
                            },
                        },
                    },
                },
            ],
        },
        "http": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "method": {"type": "string", "pattern": "^[A-Z]{1,16}$"},
                        "host": {
                            "type": "string",
                            "maxLength": 253,
                            "pattern": "^[A-Za-z0-9.:\\[\\]\\-]*$",
                        },
                        "path": {
                            "type": "string",
                            "maxLength": 512,
                            "pattern": "^[^\\x00-\\x1f\\x7f?#]*$",
                        },
                        "status": {"type": "integer", "minimum": 100, "maximum": 599},
                        "content_type": {
                            "type": ["string", "null"],
                            "maxLength": 128,
                            "pattern": "^[a-z0-9!#$&^_.+\\-]+/[a-z0-9!#$&^_.+\\-]+$",
                        },
                        "problem_type": {"type": ["string", "null"], "maxLength": 256},
                        "ucp_message_codes": {"$ref": "#/$defs/messages"},
                        "dropped_codes": {"type": "integer", "minimum": 0},
                        "elapsed_ms": {"type": ["number", "null"]},
                    },
                },
            ],
        },
        "session": {"anyOf": [{"type": "null"}, {"$ref": "#/$defs/session"}]},
        "activity": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "array",
                    "maxItems": 50,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "at": {"$ref": "#/$defs/ts"},
                            "kind": {"enum": ["command", "error", None]},
                            "command": {"$ref": "#/$defs/command"},
                            "code": {"$ref": "#/$defs/id"},
                        },
                    },
                },
            ],
        },
        "build_errors": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["section", "type"],
                "properties": {
                    "section": {"type": "string", "pattern": "^[a-z_]{1,32}$"},
                    "type": {"type": "string", "maxLength": 64, "pattern": "^[A-Za-z0-9_]*$"},
                },
            },
        },
    },
    "$defs": {
        "id": {"type": ["string", "null"], "pattern": "^[A-Za-z0-9_.:\\-]{1,160}$"},
        "token": {"type": ["string", "null"], "pattern": "^[a-z0-9_.]{1,64}$"},
        "bool": {"type": ["boolean", "null"]},
        "num": {"type": ["number", "null"]},
        "ts_required": {"type": "string", "maxLength": 48, "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}"},
        "ts": {"type": ["string", "null"], "maxLength": 48, "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}"},
        "tz": {
            "type": ["string", "null"],
            "maxLength": 128,
            "pattern": "^[A-Za-z][A-Za-z0-9_+/\\-]*$",
        },
        "label": {"type": ["string", "null"], "maxLength": 120, "pattern": "^[^\\x00-\\x1f\\x7f]*$"},
        "url": {"type": ["string", "null"], "maxLength": 300, "pattern": "^https?://[^\\s?#]+$"},
        "qualname": {"type": ["string", "null"], "pattern": "^[A-Za-z0-9_.<>]{1,200}$"},
        "command": {"type": ["string", "null"], "maxLength": 4000, "pattern": "^[a-z0-9 &\\-]*$"},
        "id_list": {"type": ["array", "null"], "items": {"$ref": "#/$defs/id"}},
        "messages": {
            "type": ["array", "null"],
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["code"],
                "properties": {
                    "code": {"type": "string", "pattern": "^[a-z0-9_.]{1,64}$"},
                    "severity": {"$ref": "#/$defs/token"},
                },
            },
        },
        "amount": {
            "anyOf": [
                {"type": ["number", "null"]},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "amount": {"$ref": "#/$defs/num"},
                        "amount_minor": {"$ref": "#/$defs/num"},
                        "minor_units": {"$ref": "#/$defs/num"},
                        "value": {"$ref": "#/$defs/num"},
                        "decimal_places": {"$ref": "#/$defs/num"},
                        "currency": {"$ref": "#/$defs/id"},
                        "model": {"$ref": "#/$defs/id"},
                        "payment_timing": {"$ref": "#/$defs/id"},
                    },
                },
            ],
        },
        "duration_leaf": {"anyOf": [{"$ref": "#/$defs/id"}, {"type": "number"}]},
        "duration": {
            "anyOf": [
                {"$ref": "#/$defs/duration_leaf"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "fixed": {"$ref": "#/$defs/duration_leaf"},
                        "min": {"$ref": "#/$defs/duration_leaf"},
                        "max": {"$ref": "#/$defs/duration_leaf"},
                    },
                },
            ],
        },
        "terms": {
            "anyOf": [
                {"type": "null"},
                {"type": "array", "maxItems": 20, "items": {"$ref": "#/$defs/terms"}},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"$ref": "#/$defs/id"},
                        "term_id": {"$ref": "#/$defs/id"},
                        "kind": {"$ref": "#/$defs/id"},
                        "type": {"$ref": "#/$defs/id"},
                        "code": {"$ref": "#/$defs/id"},
                        "timing": {"$ref": "#/$defs/id"},
                        "model": {"$ref": "#/$defs/id"},
                        "currency": {"$ref": "#/$defs/id"},
                        "amount": {"$ref": "#/$defs/amount"},
                        "price": {"$ref": "#/$defs/amount"},
                        "due_now": {"$ref": "#/$defs/amount"},
                        "amount_minor": {"$ref": "#/$defs/num"},
                        "minor_units": {"$ref": "#/$defs/num"},
                        "due_now_minor": {"$ref": "#/$defs/num"},
                        "percent": {"$ref": "#/$defs/num"},
                        "percentage": {"$ref": "#/$defs/num"},
                        "due": {"$ref": "#/$defs/ts"},
                        "due_at": {"$ref": "#/$defs/ts"},
                        "due_date": {"$ref": "#/$defs/ts"},
                        "schedule": {"$ref": "#/$defs/terms"},
                        "schedules": {"$ref": "#/$defs/terms"},
                        "installments": {"$ref": "#/$defs/terms"},
                        "payments": {"$ref": "#/$defs/terms"},
                        "items": {"$ref": "#/$defs/terms"},
                    },
                },
            ],
        },
        "slot": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "slot_id": {"$ref": "#/$defs/id"},
                "start": {"$ref": "#/$defs/ts"},
                "end": {"$ref": "#/$defs/ts"},
                "duration": {"$ref": "#/$defs/duration"},
                "price_amount": {"$ref": "#/$defs/num"},
                "alternate_slot_ids": {"$ref": "#/$defs/id_list"},
            },
        },
        "slots": {"type": ["array", "null"], "maxItems": 50, "items": {"$ref": "#/$defs/slot"}},
        "timings": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "patternProperties": {
                "^[a-z0-9_]{1,64}$": {
                    "anyOf": [
                        {"type": "number"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "patternProperties": {"^[a-z0-9_]{1,64}$": {"type": "number"}},
                        },
                    ],
                },
            },
        },
        "session": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"$ref": "#/$defs/id"},
                "created_at": {"$ref": "#/$defs/ts"},
                "schema_version": {"$ref": "#/$defs/num"},
                "flow_phase": {"$ref": "#/$defs/id"},
                "business": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "profile_url": {"$ref": "#/$defs/url"},
                        "rest_endpoint": {"$ref": "#/$defs/url"},
                        "business_name": {"$ref": "#/$defs/label"},
                        "timezone": {"$ref": "#/$defs/tz"},
                        "ucp_checkout": {"type": "boolean"},
                    },
                },
                "service_timezone": {"$ref": "#/$defs/tz"},
                "buyer_timezone": {"$ref": "#/$defs/tz"},
                "buyer_timezone_source": {"$ref": "#/$defs/id"},
                "buyer_timezone_error": {"$ref": "#/$defs/id"},
                "timezone_difference_shown": {"$ref": "#/$defs/bool"},
                "slots_presented_dual_zone": {"$ref": "#/$defs/bool"},
                "calendar_gate_completed": {"$ref": "#/$defs/bool"},
                "calendar_state": {"$ref": "#/$defs/id"},
                "calendar_mode": {"$ref": "#/$defs/id"},
                "calendar_route": {"$ref": "#/$defs/id"},
                "calendar_token_env": {"$ref": "#/$defs/id"},
                "link_auth_gate_completed": {"$ref": "#/$defs/bool"},
                "availability_window": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {"from": {"$ref": "#/$defs/ts"}, "to": {"$ref": "#/$defs/ts"}},
                },
                "slots_presented": {"$ref": "#/$defs/slots"},
                "slot_id": {"$ref": "#/$defs/id"},
                "slot_user_confirmed": {"$ref": "#/$defs/bool"},
                "service_id": {"$ref": "#/$defs/id"},
                "timezone_conflict_confirmed": {"$ref": "#/$defs/bool"},
                "live_service": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"$ref": "#/$defs/id"},
                        "service_name": {"$ref": "#/$defs/label"},
                        "duration": {"$ref": "#/$defs/duration"},
                        "price": {"$ref": "#/$defs/amount"},
                        "payment_timing": {"$ref": "#/$defs/id"},
                        "channel_types": {"$ref": "#/$defs/id_list"},
                    },
                },
                "payment_timing": {"$ref": "#/$defs/id"},
                "payment_route": {"$ref": "#/$defs/id"},
                "selected_term_id": {"$ref": "#/$defs/id"},
                "payment_schedules": {"$ref": "#/$defs/terms"},
                "due_now_amount_minor": {"$ref": "#/$defs/num"},
                "payment_disclosures": {"$ref": "#/$defs/terms"},
                "disclosures_user_confirmed": {"$ref": "#/$defs/bool"},
                "total_confirmed_amount_minor": {"$ref": "#/$defs/num"},
                "accepted_term": {"$ref": "#/$defs/terms"},
                "checkout_mode": {"$ref": "#/$defs/id"},
                "supported_checkout_modes": {"$ref": "#/$defs/id_list"},
                "checkout_prereq_completed": {"$ref": "#/$defs/bool"},
                "ucp_checkout_rest_base": {"$ref": "#/$defs/url"},
                "ucp_checkout_session_id": {"$ref": "#/$defs/id"},
                "ucp_checkout_amount_minor": {"$ref": "#/$defs/num"},
                "ucp_merchant_quote": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "patternProperties": {
                        "^[a-z0-9_]{1,64}$": {"anyOf": [{"type": ["number", "boolean"]}, {"$ref": "#/$defs/id"}]},
                    },
                },
                "ucp_checkout_messages": {"$ref": "#/$defs/messages"},
                "ucp_spt_network_id": {"$ref": "#/$defs/id"},
                "ucp_protocol_version": {"$ref": "#/$defs/id"},
                "ucp_negotiated_capabilities": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "patternProperties": {
                        "^[A-Za-z0-9_.:\\-]{1,160}$": {"type": "array", "maxItems": 10, "items": {"$ref": "#/$defs/id"}},
                    },
                },
                "ucp_capabilities_authoritative": {"$ref": "#/$defs/bool"},
                "ucp_checkout_cancel_attempted": {"$ref": "#/$defs/bool"},
                "ucp_checkout_cancel_succeeded": {"$ref": "#/$defs/bool"},
                "buyer_source": {"$ref": "#/$defs/label"},
                "booking": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "id": {"$ref": "#/$defs/id"},
                        "checkout_path": {"$ref": "#/$defs/id"},
                        "action_expires_at": {"$ref": "#/$defs/ts"},
                        "endpoint": {"$ref": "#/$defs/url"},
                        "scoped_auth_present": {"type": "boolean"},
                        "scoped_auth_expires_at": {"$ref": "#/$defs/ts"},
                        "scoped_auth_scope": {"$ref": "#/$defs/id_list"},
                        "revision": {"$ref": "#/$defs/id"},
                        "status": {"$ref": "#/$defs/id"},
                    },
                },
                "payment": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "status": {"$ref": "#/$defs/id"},
                        "spend_request_id": {"$ref": "#/$defs/id"},
                        "order_id": {"$ref": "#/$defs/id"},
                        "spend_approval_surfaced": {"$ref": "#/$defs/bool"},
                        "spend_approval_user_surfaced": {"$ref": "#/$defs/bool"},
                        "approved_at": {"$ref": "#/$defs/ts"},
                        "spend_action_surfaced": {"$ref": "#/$defs/id"},
                        "declined_spend_request_id": {"$ref": "#/$defs/id"},
                        "declined_code": {"$ref": "#/$defs/token"},
                        "declined_checkout_id": {"$ref": "#/$defs/id"},
                        "captured_unconfirmed": {"$ref": "#/$defs/bool"},
                    },
                },
                "last_error": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {"code": {"$ref": "#/$defs/id"}},
                },
                "discover_location_radius_km": {"$ref": "#/$defs/num"},
                "discover_registry_url": {"$ref": "#/$defs/url"},
                "discover_services": {
                    "type": ["array", "null"],
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "service_id": {"$ref": "#/$defs/id"},
                            "service_name": {"$ref": "#/$defs/label"},
                        },
                    },
                },
                "flow_timings": {"$ref": "#/$defs/timings"},
                "human_gate_timings": {"$ref": "#/$defs/timings"},
                "skill_compliance_validated": {"$ref": "#/$defs/bool"},
                "search_area_confirmed": {"$ref": "#/$defs/bool"},
                "service_address_set": {"$ref": "#/$defs/bool"},
                "calendar_timezone": {"$ref": "#/$defs/tz"},
                "reschedule_original_slot_id": {"$ref": "#/$defs/id"},
                "reschedule_slots": {"$ref": "#/$defs/slots"},
                "reschedule_slot_id": {"$ref": "#/$defs/id"},
                "reschedule_user_confirmed": {"$ref": "#/$defs/bool"},
            },
        },
    },
}

_KEYWORDS = frozenset({
    "$schema", "$id", "title", "description", "$defs", "$ref", "type", "const", "enum",
    "pattern", "maxLength", "minimum", "maximum", "maxItems", "items", "properties",
    "patternProperties", "additionalProperties", "required", "anyOf",
})
_DEFS_PREFIX = "#/$defs/"


class _SchemaError(Exception):
    pass


def _type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "integer" if value.is_integer() else "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_matches(expected: str, actual: str) -> bool:
    return expected == actual or (expected == "number" and actual == "integer")


def _equal(a: Any, b: Any) -> bool:
    return _type_of(a) == _type_of(b) and a == b


@lru_cache(maxsize=None)
def _regex(pattern: str) -> "re.Pattern[str]":
    # The ingest's JavaScript `$` does not match before a trailing newline; Python's does.
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.compile(pattern)


def _resolve(root: Dict[str, Any], ref: str) -> Dict[str, Any]:
    if not ref.startswith(_DEFS_PREFIX):
        raise _SchemaError(f"unsupported $ref {ref}")
    target = root.get("$defs", {}).get(ref[len(_DEFS_PREFIX):])
    if target is None:
        raise _SchemaError(f"unknown $ref {ref}")
    return target


def _check(root: Dict[str, Any], schema: Dict[str, Any], value: Any, path: str, errors: List[str]) -> None:
    unknown = set(schema) - _KEYWORDS
    if unknown:
        raise _SchemaError(f"unsupported keyword {sorted(unknown)[0]}")
    if "$ref" in schema:
        _check(root, _resolve(root, schema["$ref"]), value, path, errors)
    if "anyOf" in schema:
        def matches(option: Dict[str, Any]) -> bool:
            sub: List[str] = []
            _check(root, option, value, path, sub)
            return not sub

        if not any(matches(option) for option in schema["anyOf"]):
            errors.append(f"{path}: matches no anyOf option")
    actual = _type_of(value)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_matches(t, actual) for t in types):
            errors.append(f"{path}: expected {'|'.join(types)}, got {actual}")
            return
    if "const" in schema and not _equal(value, schema["const"]):
        errors.append(f"{path}: not {schema['const']}")
    if "enum" in schema and not any(_equal(value, option) for option in schema["enum"]):
        errors.append(f"{path}: not in enum")
    if actual == "string":
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not _regex(schema["pattern"]).search(value):
            errors.append(f"{path}: does not match pattern")
    if actual in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if actual == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                _check(root, schema["items"], item, f"{path}[{i}]", errors)
    if actual == "object":
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        properties = schema.get("properties", {})
        patterns = [(_regex(p), sub) for p, sub in schema.get("patternProperties", {}).items()]
        for key, item in value.items():
            matched = False
            if key in properties:
                matched = True
                _check(root, properties[key], item, f"{path}.{key}", errors)
            for regex, sub in patterns:
                if isinstance(key, str) and regex.search(key):
                    matched = True
                    _check(root, sub, item, f"{path}.{key}", errors)
            if not matched and schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected key")


def validate_bundle(value: Any, schema: Dict[str, Any] = SUPPORT_BUNDLE_SCHEMA) -> List[str]:
    """Every way ``value`` breaks ``schema``; empty means valid. Unsupported schema fails closed."""
    errors: List[str] = []
    try:
        _check(schema, schema, value, "$", errors)
    except (_SchemaError, RecursionError) as e:
        errors.append(f"schema: {e}")
    return errors
