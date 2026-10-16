"""Cancel and reschedule through the CLI against an HTTP business.

The agent is ``booking-agent flow``. The business is the protocol mock that speaks
USP over HTTP, including conditional writes. A revision conflict while the
booking is still ``confirmed`` must be reconciled on both sides: the agent
retries, and the business applies the write.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from booking_agent.pop import PopKey
from booking_agent.rest_client import USPBusinessRestClient
from booking_agent_cli.main import app
from booking_agent_cli.session import (
    BookingRef,
    Business,
    Session,
    SlotRef,
    add_session,
    load_session,
    load_store,
    record_issued_credential,
    save_store,
)
from support.mock.pop_transport import (
    ENDPOINT,
    PROFILE_URI,
    MockPopBusinessTransport,
    business_profile,
)

AGENT_PROFILE = "https://profile.booking-agent.dev/platform-profile.json"


def _at(days: int, hour: int) -> str:
    day = datetime.now(timezone.utc) + timedelta(days=days)
    return day.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def _invoke(runner: CliRunner, *args: str, exit_code: int = 0) -> dict:
    result = runner.invoke(app, ["flow", *args])
    text = result.stdout or result.output or ""
    assert result.exit_code == exit_code, f"stdout={text!r} exc={result.exception!r}"
    text = text.strip()
    obj, _ = json.JSONDecoder().raw_decode(text[text.index("\n{") + 1:] if not text.startswith("{") else text)
    return obj


def _posts(mock: MockPopBusinessTransport, action: str) -> list:
    return [
        request for request in mock.requests
        if request.method == "POST" and request.url.path.endswith(f"/{action}")
    ]


class _HttpBusiness:
    """The CLI's backend, forwarding to the same client the mock business serves."""

    def __init__(self, client: USPBusinessRestClient):
        self._client = client
        self._profile = business_profile()

    async def resolve_profile(self, _profile_url):
        return self._profile

    async def get_booking(self, _profile, booking_id, *, credential=None, credential_endpoint=None):
        return await self._client.get_booking(booking_id, credential=credential)

    async def cancel_booking(
        self, _profile, booking_id, reason=None, *,
        credential=None, credential_endpoint=None, if_match=None,
    ):
        return await self._client.cancel_booking(
            booking_id, reason=reason, credential=credential, if_match=if_match,
        )

    async def reschedule_booking(
        self, _profile, booking_id, new_slot_id, *,
        credential=None, credential_endpoint=None, if_match=None,
    ):
        return await self._client.reschedule_booking(
            booking_id, new_slot_id, credential=credential, if_match=if_match,
        )


async def _open_confirmed_booking(monkeypatch):
    """Create a confirmed booking, then move its revision out from under the agent."""
    mock = MockPopBusinessTransport().patch(monkeypatch)
    client = USPBusinessRestClient(
        business_profile(),
        agent_profile_url=AGENT_PROFILE,
        pop_key=PopKey.generate(),
        authenticate=True,
    )
    booking = await client.create_booking(
        service_id="svc_1",
        slot_id="slot_old",
        buyer={"name": "Ada", "email": "ada@example.com"},
    )
    # A concurrent change bumps the token and leaves the appointment confirmed.
    mock._bookings[booking.id]["revision"] = "rev-2"
    return mock, client, booking


def _seed(monkeypatch, client, booking, **session_fields) -> None:
    fields = {"calendar_state": "skipped", "calendar_mode": "none", **session_fields}
    session = Session(
        flow_phase="CONFIRMED",
        business=Business(profile_url=PROFILE_URI, name="Mock Business"),
        service_id="svc_1",
        booking=BookingRef(id=booking.id),
        **fields,
    )
    record_issued_credential(
        session,
        client.last_issued_credential,
        endpoint=ENDPOINT,
        revision=booking.revision,
    )
    # The manage page would skip the credentialed write. These tests are that write.
    session.booking.booking_url = None
    store = load_store()
    add_session(store, session)
    save_store(store)
    monkeypatch.setattr(
        "booking_agent_cli.commands.flow.resolve_backend",
        lambda: _HttpBusiness(client),
    )


def test_stale_revision_still_cancels_a_confirmed_booking(runner, isolated_session, monkeypatch):
    mock, client, booking = asyncio.run(_open_confirmed_booking(monkeypatch))
    _seed(monkeypatch, client, booking)

    result = _invoke(runner, "book", "cancel", "--reason", "changed plans")

    cancels = _posts(mock, "cancel")
    assert [request.headers.get("If-Match") for request in cancels] == ["rev-1", "rev-2"]
    assert mock._bookings[booking.id]["status"] == "canceled"
    assert mock._bookings[booking.id]["revision"] == "rev-3"
    assert result["data"]["status"] == "canceled"
    assert result["data"]["booking_id"] == booking.id
    assert load_session().booking.credential is None


def test_stale_revision_still_reschedules_a_confirmed_booking(runner, isolated_session, monkeypatch):
    old = SlotRef(slot_id="slot_old", start=_at(2, 10), end=_at(2, 11), duration="PT1H")
    new = SlotRef(slot_id="slot_new", start=_at(3, 14), end=_at(3, 15), duration="PT1H")
    mock, client, booking = asyncio.run(_open_confirmed_booking(monkeypatch))
    # The business echoes the slot it has on file. Keep that appointment in the
    # future so a successful move is not archived as already over.
    mock._bookings[booking.id]["slot"]["start"] = old.start
    mock._bookings[booking.id]["slot"]["end"] = old.end
    mock.slots["slot_new"] = {
        "start": new.start, "end": new.end, "duration": new.duration,
    }
    _seed(
        monkeypatch,
        client,
        booking,
        slots_presented=[old],
        slot_id=old.slot_id,
        slot_user_confirmed=True,
        reschedule_original_slot_id=old.slot_id,
        reschedule_slots=[new],
        reschedule_slot_id=new.slot_id,
        reschedule_user_confirmed=True,
    )
    credential = load_session().booking.credential

    result = _invoke(runner, "reschedule", "apply", "--user-confirmed")

    moves = _posts(mock, "reschedule")
    assert [request.headers.get("If-Match") for request in moves] == ["rev-1", "rev-2"]
    assert json.loads(moves[-1].content)["slot_id"] == "slot_new"
    stored = mock._bookings[booking.id]
    assert stored["status"] == "confirmed"
    assert stored["slot"]["id"] == "slot_new"
    assert stored["slot"]["start"] == new.start
    assert stored["slot"]["end"] == new.end
    assert stored["revision"] == "rev-3"
    assert result["data"]["status"] == "confirmed"
    assert result["data"]["new_slot"]["slot_id"] == "slot_new"
    after = load_session()
    assert after.slot_id == "slot_new"
    assert after.slots_presented[-1].start == new.start
    assert after.slots_presented[-1].end == new.end
    assert after.booking.revision == "rev-3"
    assert after.booking.credential == credential
    assert after.reschedule_slot_id is None


class _FakeGoogleCalendar:
    """Google Calendar events API: create, move, and delete one primary-calendar event."""

    def __init__(self):
        self.events: dict = {}
        self.calls: list = []

    def install(self, monkeypatch) -> None:
        google = self

        class Resp:
            def __init__(self, status_code, body=None):
                self.status_code = status_code
                self._body = body or {}
                self.text = json.dumps(self._body)

            def json(self):
                return self._body

        class Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, headers=None):
                google.calls.append(("POST", url))
                event_id = f"evt-{len(google.events) + 1}"
                google.events[event_id] = json
                return Resp(200, {"id": event_id, "htmlLink": f"https://calendar.google.com/{event_id}"})

            def patch(self, url, json=None, headers=None):
                google.calls.append(("PATCH", url))
                event_id = url.rsplit("/", 1)[1]
                if event_id not in google.events:
                    return Resp(404)
                google.events[event_id] = json
                return Resp(200, {"id": event_id, "htmlLink": f"https://calendar.google.com/{event_id}"})

            def delete(self, url, headers=None):
                google.calls.append(("DELETE", url))
                event_id = url.rsplit("/", 1)[1]
                return Resp(204 if google.events.pop(event_id, None) is not None else 404)

        monkeypatch.setattr("booking_agent_cli.calendar_event.httpx.Client", Client)


def test_book_reschedule_cancel_refund_and_calendar_cleanup(
    runner, isolated_session, monkeypatch, tmp_path,
):
    """The full chat: a $1.00 paid booking with a calendar event, moved, then cancelled."""
    from booking_agent_cli.calendar_event import existing_event_id, write_token_cache
    from booking_agent_cli.session import PaymentRef

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GOOGLE_CALENDAR_TOKEN_CACHE_DIR", raising=False)
    write_token_cache("cli", "tok-write", expires_in=3600)
    google = _FakeGoogleCalendar()
    google.install(monkeypatch)

    old = SlotRef(
        slot_id="slot_old", start=_at(2, 11), end=_at(2, 12), duration="PT1H", price_amount=100,
    )
    new = SlotRef(
        slot_id="slot_new", start=_at(2, 13), end=_at(2, 14), duration="PT1H", price_amount=100,
    )
    mock, client, booking = asyncio.run(_open_confirmed_booking(monkeypatch))
    mock._bookings[booking.id]["slot"]["start"] = old.start
    mock._bookings[booking.id]["slot"]["end"] = old.end
    mock.slots["slot_new"] = {"start": new.start, "end": new.end, "duration": new.duration}
    _seed(
        monkeypatch,
        client,
        booking,
        calendar_state="connected",
        calendar_mode="events",
        payment_route="at_booking",
        payment=PaymentRef(status="paid", order_id="o_1"),
        ucp_checkout_amount_minor=100,
        ucp_merchant_quote={"currency": "USD"},
        live_service={"name": "AC Repair"},
        slots_presented=[old],
        slot_id=old.slot_id,
        slot_user_confirmed=True,
    )
    session_id = load_session().id

    created = _invoke(runner, "calendar", "create-event")
    event_id = created["data"]["event_id"]
    assert existing_event_id(session_id) == event_id
    assert google.events[event_id]["start"]["dateTime"].startswith(old.start[:16])

    session = load_session()
    session.reschedule_original_slot_id = old.slot_id
    session.reschedule_slots = [new]
    session.reschedule_slot_id = new.slot_id
    session.reschedule_user_confirmed = True
    store = load_store()
    store.sessions = [session if s.id == session.id else s for s in store.sessions]
    save_store(store)

    moved = _invoke(runner, "reschedule", "apply", "--user-confirmed")
    assert moved["next_command"] == "booking-agent flow calendar update-event"
    _invoke(runner, "calendar", "update-event")
    assert list(google.events) == [event_id]
    assert google.events[event_id]["start"]["dateTime"].startswith(new.start[:16])

    cancelled = _invoke(runner, "book", "cancel", "--reason", "changed plans")

    assert mock._bookings[booking.id]["status"] == "canceled"
    follow_up = cancelled["data"]["refund_follow_up"]
    assert follow_up["outstanding_display"] == "$1.00"
    message = cancelled["user_message"]
    assert message.startswith("Your booking with Mock Business on ")
    assert "T13:00" not in message.splitlines()[0]
    assert "did not say whether the $1.00 you paid will be refunded. To get it back:" in message
    assert "2. Ask them to refund the $1.00 you paid if it was not refunded automatically." in message
    assert f"3. Give them booking ID {booking.id} and order reference o_1" in message
    assert message.endswith("I also removed it from your Google Calendar.")
    assert cancelled["data"]["calendar"]["status"] == "deleted"
    assert google.events == {}
    assert ("DELETE", f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{event_id}") in google.calls
    assert existing_event_id(session_id) is None
