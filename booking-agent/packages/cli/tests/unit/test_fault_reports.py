"""Automatic reports of faults the user never saw: what they may carry and when they go."""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from booking_agent import faults
from booking_agent_cli import fault_reports
from booking_agent_cli.fault_report_schema import FAULT_REPORT_SCHEMA
from booking_agent_cli.support_bundle_schema import validate_bundle

_SRC = Path(__file__).resolve().parents[2] / "src"
_INGEST = "https://script.google.com/macros/s/x/exec"
_ECHO = "https://script.googleusercontent.com/macros/echo?user_content_key=abc"

_BUYER = {
    "name": "Ada Lovelace",
    "email": "ada.lovelace@example.org",
    "phone": "+15555550123",
    "address": "742 Evergreen Terrace, Springfield",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_SESSION_DIR", str(tmp_path))
    monkeypatch.setenv("BOOKING_AGENT_FAULT_REPORTS", "1")
    monkeypatch.setattr(fault_reports, "_home", lambda: tmp_path)
    monkeypatch.setattr(
        "booking_agent_cli.support_report.DEFAULT_SUPPORT_INGEST_URL", _INGEST,
    )
    return tmp_path


def _ingest(replies):
    """A fake Apps Script: POST redirects to the echo host, which answers from ``replies``."""
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(302, headers={"location": _ECHO})
        return httpx.Response(200, json=replies.pop(0) if replies else {"accepted": True})

    return httpx.MockTransport(handler), posted


def _profile_failure():
    from booking_agent.ucp_profile import UcpProfileValidationError

    return UcpProfileValidationError("UCP profile has no USP service binding or catalog capability")


# ---------------------------------------------------------------------------
# What a report may carry
# ---------------------------------------------------------------------------

def test_no_user_value_reaches_a_report_from_any_source(monkeypatch):
    """Buyer data in exception text, log arguments, and command values never leaks."""
    leaky = ValueError(f"failed for {_BUYER['email']} at {_BUYER['address']}")
    faults.record_silent_fault(
        "profile_resolve", leaky,
        profile_url=f"https://biz.test/.well-known/ucp?email={_BUYER['email']}",
        business_id="biz-1",
    )
    faults.record_silent_fault("catalog_enrich", _profile_failure(), service_id="svc-1")
    collector = faults._WarningCollector()
    logger = logging.getLogger("booking_agent.backend.live")
    logger.addHandler(collector)
    try:
        logger.warning("Skipping result for %s (%s)", _BUYER["name"], _BUYER["phone"])
    finally:
        logger.removeHandler(collector)
    monkeypatch.setattr(
        "sys.argv",
        ["booking-agent", "flow", "book", "--notes", _BUYER["address"], "--user-confirmed"],
    )
    fault_reports.note_error("booking_confirmation_required")

    reports = fault_reports.collect()

    assert {r["fault_class"] for r in reports} == {
        "absorbed_exception", "degraded_result", "agent_contract_violation",
    }
    payload = json.dumps(reports)
    for value in _BUYER.values():
        assert value not in payload
    assert "?" not in payload.replace('"', "")
    for report in reports:
        assert validate_bundle(report, FAULT_REPORT_SCHEMA) == []
    violation = next(r for r in reports if r["fault_class"] == "agent_contract_violation")
    assert violation["command"] == "booking-agent flow book --notes --user-confirmed"


def test_a_failure_the_user_was_told_about_stays_on_the_consented_path():
    faults.record_silent_fault("profile_resolve", _profile_failure(), business_id="biz-1")
    fault_reports.note_error("payment_failed")
    fault_reports.note_error("policies_not_accepted")

    reports = fault_reports.collect()

    assert [r["fault_class"] for r in reports] == ["agent_contract_violation"]


def test_an_unusable_business_profile_during_discovery_is_one_report(monkeypatch):
    """Replay of a registry business whose UCP profile has no USP binding."""
    from booking_agent.backend.live import LiveUSPBackend
    from booking_agent.headless import build_search_intent
    from booking_agent.models.platform import ServiceSearchResult
    from booking_agent.models.wire import RegistryBusinessInfo

    def hit(service_id):
        return ServiceSearchResult(
            service_id=service_id,
            service_name=service_id,
            business=RegistryBusinessInfo(
                id="reg_24a38331a3083e478d8ec4733303c55b",
                name="AZ Home Technologies",
                profile_url="https://www.azhometechusa.com/.well-known/ucp",
            ),
            category="hvac",
        )

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def no_pacing(_host):
        yield

    backend = LiveUSPBackend()
    backend._cfg.usp_registry_url = "https://registry.test/search"
    monkeypatch.setattr(
        "booking_agent.backend.live.USPRegistryRestClient.search_registry_for_services",
        AsyncMock(return_value=([hit("svc-roof"), hit("svc-consult")], MagicMock())),
    )
    monkeypatch.setattr(
        "booking_agent.backend.live.fetch_and_resolve_business_profile",
        AsyncMock(side_effect=_profile_failure()),
    )
    monkeypatch.setattr("booking_agent.profile_cache.read_cached_profile", lambda _url: None)
    monkeypatch.setattr("booking_agent.backend.live.paced_merchant_host_request", no_pacing)

    import asyncio

    services = asyncio.run(backend.search_registry(build_search_intent(query="ac repair")))
    reports = fault_reports.collect()

    assert all(s.metadata["profile_resolved"] is False for s in services)
    assert len(reports) == 1
    report = reports[0]
    assert report["step"] == "profile_resolve"
    assert report["reason_code"] == "ucp_profile_has_no_usp_service_binding_or_catalog_capability"
    assert report["subject"] == {
        "host": "www.azhometechusa.com",
        "path": "/.well-known/ucp",
        "business_id": "reg_24a38331a3083e478d8ec4733303c55b",
    }


# ---------------------------------------------------------------------------
# Queue and send
# ---------------------------------------------------------------------------

def test_reports_are_sent_after_the_command_and_not_repeated_within_a_day(home):
    transport, posted = _ingest([{"accepted": True}])
    faults.record_silent_fault("profile_resolve", _profile_failure(), business_id="biz-1")

    fault_reports.finish_command(transport=transport)

    assert [p["kind"] for p in posted] == ["fault_report"]
    assert not (home / fault_reports.QUEUE_NAME).exists()
    fingerprint = posted[0]["fingerprint"]
    assert fingerprint in json.loads((home / fault_reports.SENT_NAME).read_text())

    faults.record_silent_fault("profile_resolve", _profile_failure(), business_id="biz-1")
    fault_reports.finish_command(transport=transport)
    assert len(posted) == 1


def test_a_report_the_ingest_could_not_take_waits_for_the_next_command(home):
    transport, posted = _ingest([{"accepted": False, "reason": "rate_limited"}])
    faults.record_silent_fault("profile_resolve", _profile_failure(), business_id="biz-1")

    fault_reports.finish_command(transport=transport)

    queued = (home / fault_reports.QUEUE_NAME).read_text().splitlines()
    assert len(queued) == 1 and len(posted) == 1
    fault_reports.finish_command(transport=transport)
    assert len(posted) == 2
    assert not (home / fault_reports.QUEUE_NAME).exists()


def test_a_report_the_ingest_rejects_for_its_schema_is_dropped(home):
    transport, _posted = _ingest([{"accepted": False, "reason": "schema"}])
    faults.record_silent_fault("profile_resolve", _profile_failure(), business_id="biz-1")

    fault_reports.finish_command(transport=transport)

    assert not (home / fault_reports.QUEUE_NAME).exists()


def test_opting_out_sends_and_stores_nothing(home, monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_FAULT_REPORTS", "0")

    def boom(_request):
        raise AssertionError("nothing may be sent after opting out")

    faults.record_silent_fault("profile_resolve", _profile_failure(), business_id="biz-1")
    fault_reports.note_error("calendar_offer_not_shown")
    fault_reports.finish_command(transport=httpx.MockTransport(boom))

    assert not (home / fault_reports.QUEUE_NAME).exists()
    assert faults.drain() == []


def test_the_queue_stays_bounded(home):
    reports = [
        fault_reports.project({"fault_class": "degraded_result", "step": "cli", "message_template": f"m{i}"})
        for i in range(fault_reports.MAX_QUEUE + 10)
    ]
    fault_reports.enqueue(reports)

    queued = (home / fault_reports.QUEUE_NAME).read_text().splitlines()
    assert len(queued) == fault_reports.MAX_QUEUE


# ---------------------------------------------------------------------------
# Guards on the code that feeds reports
# ---------------------------------------------------------------------------

def _handlers(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == "Exception":
            yield node


def test_every_absorbing_handler_in_discovery_records_the_fault():
    path = _SRC / "booking_agent" / "backend" / "live.py"
    missing = []
    for handler in _handlers(path):
        reraises = any(isinstance(stmt, ast.Raise) for stmt in handler.body)
        records = any(
            isinstance(n, ast.Call) and getattr(n.func, "id", None) == "record_silent_fault"
            for n in ast.walk(handler)
        )
        if not (reraises or records):
            missing.append(handler.lineno)
    # Handlers that hand the failure on (a rate limit, a skipped hit) are listed by line.
    assert missing == [], f"except Exception without record_silent_fault at lines {missing}"


@pytest.mark.parametrize("logger_name", faults.WARNING_LOGGERS)
def test_reported_warnings_use_fixed_format_strings(logger_name):
    """Only the format string is reported, so it must never be built from runtime values."""
    path = _SRC.joinpath(*logger_name.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("warning", "error", "exception", "critical")
            and getattr(node.func.value, "id", None) == "logger"
        ):
            first = node.args[0] if node.args else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                bad.append(node.lineno)
    assert bad == []
