"""Observability: correlation context, events, and redaction."""
from __future__ import annotations

import httpx

from booking_agent.observability import (
    HEADER,
    bind,
    current,
    emit_event,
    payment_decline_reason_class,
    registry_error_class,
    reset,
    reset_event_sink,
    set_event_sink,
)


def test_bind_and_current_round_trip():
    token = bind("corr-abc")
    try:
        assert current() == "corr-abc"
    finally:
        reset(token)
    assert current() is None


def test_emit_event_includes_correlation_id():
    captured: list[dict] = []
    set_event_sink(captured.append)
    token = bind("corr-event")
    try:
        emit_event("registry_search", ok=True, latency_ms=12.3)
    finally:
        reset(token)
        reset_event_sink()
    assert captured[0]["event"] == "registry_search"
    assert captured[0]["correlation_id"] == "corr-event"
    assert captured[0]["ok"] is True


def test_emit_event_strips_forbidden_fields():
    captured: list[dict] = []
    set_event_sink(captured.append)
    emit_event(
        "payment_declined",
        email="ada@test.com",
        shared_payment_token="spt_secret",
        reason_class="declined",
    )
    reset_event_sink()
    assert "email" not in captured[0]
    assert "shared_payment_token" not in captured[0]
    assert captured[0]["reason_class"] == "declined"


def test_registry_error_class_mapping():
    assert registry_error_class(httpx.Response(401, request=httpx.Request("GET", "http://x"))) == "auth_rejection"
    assert registry_error_class(httpx.Response(400, request=httpx.Request("GET", "http://x"))) == "malformed_request"
    assert registry_error_class(httpx.Response(503, request=httpx.Request("GET", "http://x"))) == "upstream_failure"


def test_payment_decline_reason_class():
    assert payment_decline_reason_class(402, "card declined") == "declined"
    assert payment_decline_reason_class(500, "processor error") == "processor_error"
    assert payment_decline_reason_class(418, "unknown") == "unknown"


def test_header_constant():
    assert HEADER == "USP-Correlation-Id"
