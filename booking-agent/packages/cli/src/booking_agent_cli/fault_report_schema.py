"""Schema of the automatic fault report, checked before a report is queued.

A fault report is sent without asking the user, so it may hold only facts about the
client and about a business's public profile, never anything about the user.
The support ingest checks every report against ``FAULT_REPORT_SCHEMA`` with the same
JSON Schema subset that ``validate_bundle`` implements.
"""
from __future__ import annotations

from typing import Any, Dict

FAULT_CLASSES = ("absorbed_exception", "degraded_result", "agent_contract_violation")

FAULT_REPORT_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://booking-agent.dev/schemas/fault-report-v1.json",
    "title": "Booking Agent automatic fault report, report_version 1",
    "description": "Sent without a consent prompt, so it carries no user data. Unknown keys are rejected at every level.",
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "report_version", "created_at", "fault_class", "step", "fingerprint", "env"],
    "properties": {
        "kind": {"const": "fault_report"},
        "report_version": {"const": 1},
        "created_at": {"type": "string", "maxLength": 48, "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}"},
        "fault_class": {"enum": list(FAULT_CLASSES)},
        "step": {"type": "string", "pattern": "^[a-z0-9_]{1,40}$"},
        "exception_type": {"$ref": "#/$defs/qualname"},
        "message_template": {"$ref": "#/$defs/label"},
        "reason_code": {"$ref": "#/$defs/token"},
        "error_code": {"$ref": "#/$defs/token"},
        "command": {
            "type": ["string", "null"],
            "maxLength": 400,
            "pattern": "^booking-agent( [a-z][a-z0-9-]*| --[a-z][a-z0-9-]*)*$",
        },
        "subject": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "host": {"type": "string", "maxLength": 253, "pattern": "^[A-Za-z0-9.\\-]+$"},
                "path": {"type": "string", "maxLength": 300, "pattern": "^/[^\\s?#]*$"},
                "business_id": {"$ref": "#/$defs/id"},
                "service_ids": {"type": "array", "maxItems": 10, "items": {"$ref": "#/$defs/id"}},
                "http_status": {"type": "integer", "minimum": 100, "maximum": 599},
                "logger": {"$ref": "#/$defs/qualname"},
            },
        },
        "env": {
            "type": "object",
            "additionalProperties": False,
            "required": ["cli_version"],
            "properties": {
                "cli_version": {"$ref": "#/$defs/id"},
                "skill_version": {"$ref": "#/$defs/id"},
                "os_family": {"enum": ["Darwin", "Linux", "Windows", "other", None]},
            },
        },
        "fingerprint": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
    },
    "$defs": {
        "id": {"type": ["string", "null"], "pattern": "^[A-Za-z0-9_.:\\-]{1,160}$"},
        "token": {"type": ["string", "null"], "pattern": "^[a-z0-9_.]{1,64}$"},
        "label": {"type": ["string", "null"], "maxLength": 300, "pattern": "^[^\\x00-\\x1f\\x7f]*$"},
        "qualname": {"type": ["string", "null"], "pattern": "^[A-Za-z0-9_.<>]{1,200}$"},
    },
}
