"""Flow correlation id persistence, headers, envelope, and money-path events."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import StringIO

import httpx
import pytest
from typer.testing import CliRunner

from booking_agent.observability import HEADER, bind, reset, set_event_sink
from booking_agent_cli.main import app
from booking_agent_cli.output import AgentOutput, emit
from booking_agent_cli.session import (
    BookingRef,
    Buyer,
    PaymentRef,
    Session,
    SlotRef,
    archive_expired_session,
    bind_session_observability,
    load_store,
    save_store,
)


def test_session_correlation_id_generated_on_init():
    session = Session(user_request="haircut")
    assert session.correlation_id
    assert session.correlation_id != session.id


def test_correlation_id_stable_across_save_load(isolated_session):
    runner = CliRunner()
    r1 = runner.invoke(
        app,
        ["flow", "init", "--user-request", "haircut", "--force"],
        catch_exceptions=False,
    )
    assert r1.exit_code == 0
    store = load_store()
    session = store.sessions[-1]
    cid = session.correlation_id

    r2 = runner.invoke(app, ["flow", "status"], catch_exceptions=False)
    assert r2.exit_code == 0
    body = json.loads(r2.stdout)
    assert body.get("correlation_id") == cid


def test_old_session_file_gets_correlation_id_on_load(isolated_session):
    store = load_store()
    session = Session(user_request="legacy")
    object.__setattr__(session, "correlation_id", "")
    store.sessions.append(session)
    store.active_session_id = session.id
    save_store(store)

    reloaded = load_store()
    loaded = reloaded.sessions[-1]
    assert loaded.correlation_id


def test_archive_keeps_correlation_id():
    end = datetime.now(timezone.utc) - timedelta(hours=1)
    start = end - timedelta(hours=1)
    session = Session(
        correlation_id="corr-keep",
        flow_phase="CONFIRMED",
        slot_id="slot-1",
        slots_presented=[SlotRef(slot_id="slot-1", start=start.isoformat(), end=end.isoformat())],
        booking=BookingRef(id="bk_1", credential="bsc_secret"),
        buyer=Buyer(name="Ada", email="ada@test.com"),
        payment=PaymentRef(
            spend_request_id="sr_dispute",
            order_id="ord_1",
            status="paid",
            spend_approval_surfaced=True,
            spend_approval_user_surfaced=True,
            approved_at="2026-01-01T12:00:00+00:00",
        ),
    )
    assert archive_expired_session(session)
    assert session.correlation_id == "corr-keep"
    assert session.booking.credential is None
    assert session.buyer.email == ""
    assert session.payment.spend_request_id == "sr_dispute"
    assert session.payment.order_id == "ord_1"
    assert session.payment.status == "paid"
    assert session.payment.approved_at == "2026-01-01T12:00:00+00:00"
    assert session.payment.spend_approval_surfaced is False
    assert session.payment.spend_approval_user_surfaced is False


@pytest.mark.asyncio
async def test_traced_client_sends_correlation_header():
    from booking_agent.http_trace import traced_async_client

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    token = bind("corr-http")
    try:
        transport = httpx.MockTransport(handler)
        async with traced_async_client(transport=transport) as client:
            await client.get("https://registry.example/search")
    finally:
        reset(token)

    assert seen[0].headers[HEADER] == "corr-http"


@pytest.mark.asyncio
async def test_traced_client_succeeds_when_peer_omits_correlation_header():
    """Non-Wix USP peers may ignore unknown headers. Missing echo is not an error."""
    from booking_agent.http_trace import traced_async_client

    def handler(request: httpx.Request) -> httpx.Response:
        assert HEADER in request.headers
        return httpx.Response(200, json={"ok": True})

    token = bind("corr-no-echo")
    try:
        transport = httpx.MockTransport(handler)
        async with traced_async_client(transport=transport) as client:
            response = await client.post("https://business.example/bookings")
    finally:
        reset(token)

    assert response.status_code == 200
    assert HEADER not in response.headers


@pytest.mark.asyncio
async def test_traced_client_does_not_adopt_peer_correlation_id():
    """Never overwrite the flow id with a value a peer puts on the response."""
    from booking_agent.http_trace import traced_async_client
    from booking_agent.observability import current

    hop = {"n": 0}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers[HEADER])
        hop["n"] += 1
        if hop["n"] == 1:
            return httpx.Response(
                200,
                headers={HEADER: "corr-from-peer"},
                json={},
            )
        return httpx.Response(200, json={})

    token = bind("corr-ours")
    try:
        transport = httpx.MockTransport(handler)
        async with traced_async_client(transport=transport) as client:
            await client.get("https://business.example/availability")
            assert current() == "corr-ours"
            await client.get("https://business.example/availability")
            assert current() == "corr-ours"
    finally:
        reset(token)

    assert seen == ["corr-ours", "corr-ours"]


@pytest.mark.asyncio
async def test_unbound_traced_client_omits_correlation_header():
    from booking_agent.http_trace import traced_async_client

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    reset()
    async with traced_async_client(transport=httpx.MockTransport(handler)) as client:
        await client.get("https://business.example/health")

    assert HEADER not in seen[0].headers


def test_emit_fills_correlation_id_on_envelope():
    stream = StringIO()
    token = bind("corr-emit")
    try:
        emit(AgentOutput(phase="PAYMENT_PENDING"), stream=stream)
    finally:
        reset(token)

    payload = json.loads(stream.getvalue())
    assert payload["correlation_id"] == "corr-emit"


def test_bind_session_observability():
    session = Session(correlation_id="corr-bind")
    bind_session_observability(session)
    from booking_agent.observability import current

    assert current() == "corr-bind"
    reset()


@pytest.mark.asyncio
async def test_registry_search_emits_latency_event():
    from booking_agent.rest_client import USPRegistryRestClient

    captured: list[dict] = []
    set_event_sink(captured.append)
    token = bind("corr-reg")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "pagingMetadata": {}})

    import booking_agent.rest_client as rc

    original = rc.traced_async_client

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    rc.traced_async_client = client_factory
    try:
        await USPRegistryRestClient.search_registry_for_services(
            "https://registry.example/search",
            "haircut",
        )
    finally:
        rc.traced_async_client = original
        reset(token)
        from booking_agent.observability import reset_event_sink

        reset_event_sink()

    assert captured[-1]["event"] == "registry_search"
    assert captured[-1]["ok"] is True
    assert "latency_ms" in captured[-1]
