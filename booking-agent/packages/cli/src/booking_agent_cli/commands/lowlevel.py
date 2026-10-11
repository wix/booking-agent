"""Low-level operator commands.

These bypass the flow engine and do NOT read or mutate ``.usp-session.json``.
Documented as repair / debug tools only.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import typer

from ..secrets.demo import load_env_file, resolve_backend

# search ---------------------------------------------------------------------

search_app = typer.Typer(help="Low-level: search the USP registry (no session writes).")


@search_app.callback(invoke_without_command=True)
def search(
    ctx: typer.Context,
    query: str = typer.Option(..., "--query", "-q"),
    location: Optional[str] = typer.Option(
        None,
        "--location",
        help="Optional geo filter as latitude,longitude",
    ),
    radius_km: Optional[float] = typer.Option(
        None,
        "--radius-km",
        help="Search radius in km when --location is set",
    ),
    vertical: Optional[List[str]] = typer.Option(
        None,
        "--vertical",
        help="Repeatable registry vertical filter; omit to search all verticals",
    ),
    desired_at: Optional[List[str]] = typer.Option(None, "--desired-at"),
    desired_from: Optional[List[str]] = typer.Option(None, "--desired-from"),
    desired_range: Optional[List[str]] = typer.Option(None, "--desired-range"),
    prefer_sooner_availability_slots: Optional[bool] = typer.Option(
        None,
        "--prefer-sooner-availability-slots/--no-prefer-sooner-availability-slots",
    ),
    env_file: Optional[str] = typer.Option(None, "--env-file"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if env_file:
        load_env_file(env_file)
    backend = resolve_backend()

    async def _go():
        from booking_agent.headless import (
            build_search_intent,
            build_desired_service_time_preferences,
            discover_services,
            parse_service_search_location,
            parse_service_verticals,
        )
        from booking_agent.models.platform import ServiceSearchLocation

        loc = None
        if location and location.strip():
            parsed = parse_service_search_location(location)
            if radius_km is not None:
                loc = ServiceSearchLocation(
                    coordinates=parsed.coordinates,
                    radius_km=radius_km,
                )
            else:
                loc = parsed
        time_preferences = build_desired_service_time_preferences(
            desired_at=desired_at,
            desired_from=desired_from,
            desired_range=desired_range,
        )
        return await discover_services(
            backend,
            build_search_intent(
                query=query,
                location=loc,
                verticals=parse_service_verticals(vertical),
                desired_service_time_ranges=time_preferences or None,
                prefer_sooner_availability_slots=prefer_sooner_availability_slots,
            ),
        )

    try:
        services = asyncio.run(_go())
    except ValueError as exc:
        typer.echo(json.dumps({"error": str(exc)}), err=True)
        raise typer.Exit(code=1) from exc
    from ..catalog import discover_summary_from_service, sort_discover_summaries_by_rank

    summaries = []
    for rank, service in enumerate(services):
        row = discover_summary_from_service(service)
        if row.get("search_rank") is None:
            row["search_rank"] = rank
        summaries.append(row)
    typer.echo(json.dumps(sort_discover_summaries_by_rank(summaries), indent=2))


# profile --------------------------------------------------------------------

profile_app = typer.Typer(help="Low-level: profile resolution.")


@profile_app.command("resolve")
def profile_resolve(
    profile_url: str = typer.Option(..., "--profile-url"),
    env_file: Optional[str] = typer.Option(None, "--env-file"),
) -> None:
    if env_file:
        load_env_file(env_file)
    backend = resolve_backend()

    async def _go():
        return await backend.resolve_profile(profile_url)

    profile = asyncio.run(_go())
    typer.echo(json.dumps({
        "version": profile.version,
        "rest_endpoint": profile.rest_endpoint,
        "business": profile.business.model_dump(by_alias=True),
        "has_catalog": profile.has_catalog,
        "has_availability": profile.has_availability,
        "has_bookings": profile.has_bookings,
    }, indent=2))


# availability ---------------------------------------------------------------

availability_app = typer.Typer(help="Low-level: query availability.")


@availability_app.command("query")
def availability_query(
    profile_url: str = typer.Option(..., "--profile-url"),
    service_id: str = typer.Option(..., "--service-id"),
    start: Optional[str] = typer.Option(None, "--from"),
    end: Optional[str] = typer.Option(None, "--to"),
    env_file: Optional[str] = typer.Option(None, "--env-file"),
) -> None:
    if env_file:
        load_env_file(env_file)
    now = datetime.now(timezone.utc)
    window_from = start or (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    window_to = end or (now + timedelta(days=14)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    backend = resolve_backend()

    async def _go():
        from booking_agent.headless import query_availability_with_quirk
        profile = await backend.resolve_profile(profile_url)
        slots = await query_availability_with_quirk(
            backend, profile, service_id, window_from, window_to
        )
        return slots

    slots = asyncio.run(_go())
    typer.echo(json.dumps(
        [{"slot_id": s.id, "start": s.start, "end": s.end, "duration": s.duration} for s in slots],
        indent=2,
    ))


# booking --------------------------------------------------------------------

booking_app = typer.Typer(help="Low-level: booking create (operator repair).")


@booking_app.command("create")
def booking_create(
    profile_url: str = typer.Option(..., "--profile-url"),
    service_id: str = typer.Option(..., "--service-id"),
    slot_id: str = typer.Option(..., "--slot-id"),
    buyer_json: str = typer.Option(..., "--buyer", help="JSON {first_name, last_name, email}"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    delivery_address: Optional[str] = typer.Option(
        None,
        "--delivery-address",
        help="JSON DeliveryAddress (line1, city, country required).",
    ),
    env_file: Optional[str] = typer.Option(None, "--env-file"),
) -> None:
    if env_file:
        load_env_file(env_file)
    try:
        buyer = json.loads(buyer_json)
    except json.JSONDecodeError as e:
        typer.echo(f"Invalid --buyer JSON: {e}", err=True)
        raise typer.Exit(code=1)
    delivery_addr = None
    if delivery_address is not None:
        try:
            raw = json.loads(delivery_address)
        except json.JSONDecodeError as e:
            typer.echo(f"Invalid --delivery-address JSON: {e}", err=True)
            raise typer.Exit(code=1)
        from booking_agent.models.wire import DeliveryAddress

        delivery_addr = DeliveryAddress.model_validate(raw).model_dump(
            by_alias=False, exclude_none=True
        )
    backend = resolve_backend()

    async def _go():
        profile = await backend.resolve_profile(profile_url)
        return await backend.create_booking(
            profile=profile,
            service_id=service_id,
            slot_id=slot_id,
            buyer=buyer,
            notes=notes,
            delivery_address=delivery_addr,
        )

    booking = asyncio.run(_go())
    typer.echo(booking.model_dump_json(by_alias=True, exclude_none=True, indent=2))
