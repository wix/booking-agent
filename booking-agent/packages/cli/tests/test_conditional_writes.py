"""Conditional writes: `revision` -> `If-Match` -> 412 (USP Section 5.6).

This chain was plumbed end to end and completely inert before the fix: the
`Booking` model had no `revision` field, so the token was dropped on parse, the
`getattr(booking, "revision", None)` that fed `If-Match` always returned None,
and the header was never sent. These tests pin each link so it cannot go quiet
again.
"""
from __future__ import annotations

import pytest

from booking_agent.pop import PopKey
from booking_agent.rest_client import RevisionMismatch, USPBusinessRestClient
from support.mock.pop_transport import MockPopBusinessTransport, business_profile


def _client(monkeypatch):
    mock = MockPopBusinessTransport().patch(monkeypatch)
    key = PopKey.generate()
    client = USPBusinessRestClient(
        business_profile(), agent_profile_url="https://agent.example/p.json",
        pop_key=key, authenticate=True,
    )
    return client, mock


async def _book(client):
    booking = await client.create_booking(
        service_id="svc_1", slot_id="slot_1", buyer={"name": "A", "email": "a@b.c"},
    )
    return booking, client.last_issued_credential


@pytest.mark.asyncio
async def test_revision_survives_parsing(monkeypatch):
    """Link 1: the business's token must reach the model at all."""
    client, _ = _client(monkeypatch)
    booking, _ = await _book(client)
    assert booking.revision == "rev-1", (
        "the concurrency token was dropped on parse; If-Match can never be sent"
    )


@pytest.mark.asyncio
async def test_if_match_is_sent_when_a_revision_is_known(monkeypatch):
    """Link 2: a known revision becomes the precondition on a mutating call."""
    client, mock = _client(monkeypatch)
    booking, credential = await _book(client)

    await client.cancel_booking(
        booking.id, credential=credential, if_match=booking.revision,
    )
    assert mock.requests[-1].headers.get("If-Match") == "rev-1"


@pytest.mark.asyncio
async def test_no_if_match_when_the_business_advertises_no_revision(monkeypatch):
    """Presence of `revision` IS the advertisement; absence means unconditional.

    Same contract as a missing HTTP ETag -- we must not invent a precondition a
    business never offered, or every write would fail against it.
    """
    client, mock = _client(monkeypatch)
    booking, credential = await _book(client)

    await client.cancel_booking(booking.id, credential=credential, if_match=None)
    assert "If-Match" not in mock.requests[-1].headers


@pytest.mark.asyncio
async def test_stale_revision_is_rejected_and_the_write_does_not_happen(monkeypatch):
    """Link 3: a stale precondition raises, distinctly, and changes nothing."""
    client, mock = _client(monkeypatch)
    booking, credential = await _book(client)

    with pytest.raises(RevisionMismatch) as caught:
        await client.cancel_booking(
            booking.id, credential=credential, if_match="rev-stale",
        )
    assert caught.value.sent_revision == "rev-stale"

    # The booking is untouched -- 412 means the write was not applied.
    still_there = await client.get_booking(booking.id, credential=credential)
    assert still_there.status.value != "canceled"
    assert still_there.revision == "rev-1"


@pytest.mark.asyncio
async def test_re_reading_yields_a_usable_revision(monkeypatch):
    """The recovery the error instructs: re-read, reconcile, retry once.

    Retrying without re-reading would 412 again; dropping the precondition to
    force it through would clobber the concurrent change the precondition exists
    to protect.
    """
    client, mock = _client(monkeypatch)
    booking, credential = await _book(client)

    with pytest.raises(RevisionMismatch):
        await client.cancel_booking(
            booking.id, credential=credential, if_match="rev-stale",
        )

    fresh = await client.get_booking(booking.id, credential=credential)
    cancelled = await client.cancel_booking(
        booking.id, credential=credential, if_match=fresh.revision,
    )
    assert cancelled.status.value == "canceled"
    # A mutation must move the token on, or the next write would race.
    assert cancelled.revision != fresh.revision


@pytest.mark.asyncio
async def test_revision_is_refreshed_on_every_read(monkeypatch):
    """A revision captured at creation goes stale as soon as anything mutates.

    Polling is the client's default confirmation path, so the poll is where the
    token has to be kept current -- otherwise the first conditional write after
    a status change always 412s.
    """
    from booking_agent_cli.booking_poll import apply_booking_status
    from booking_agent_cli.session import Session

    client, _ = _client(monkeypatch)
    booking, credential = await _book(client)

    session = Session()
    apply_booking_status(session, booking)
    assert session.booking.revision == "rev-1"

    cancelled = await client.cancel_booking(
        booking.id, credential=credential, if_match=booking.revision,
    )
    apply_booking_status(session, cancelled)
    assert session.booking.revision == cancelled.revision != "rev-1"
