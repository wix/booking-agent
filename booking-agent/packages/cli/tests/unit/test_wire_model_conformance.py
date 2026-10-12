"""Every wire model declares every property its spec schema declares.

The bug that motivated this: ``Booking`` was missing ``revision``, the
concurrency token for conditional writes.  Nothing failed.  Pydantic's default
``extra="ignore"`` silently dropped it during parsing, so the field simply never
arrived, a ``getattr(booking, "revision", None)`` downstream always returned
``None``, and the ``If-Match`` header it fed was never sent.  A whole feature was
wired end to end and inert, and no test could see it, because *absence* is
exactly what an ignore-extras model cannot report.

That failure mode is generic: any property the spec adds is invisible to this
client until someone notices by hand.  So this test compares each model against
the vendored spec schema and fails on a property the spec declares and the model
does not.

Deliberate divergences are allowed but must be *written down* in
``KNOWN_DIVERGENCES`` with a reason.  An allowlist that costs nothing to extend
would be worthless; the point is that dropping a spec field becomes a visible,
argued decision rather than an oversight.

Schemas are vendored (``spec_schemas/``, see its ``SOURCE`` for the pinned
commit) so this runs in CI without a second checkout.  ``test_vendored_schemas``
guards the pin itself.
"""
from __future__ import annotations

import json
import pathlib
from typing import Dict, Iterable, Set, Tuple

import pytest
from pydantic import BaseModel

from booking_agent import discovery, ucp_profile
from booking_agent.models import platform as platform_models
from booking_agent.models import wire

SCHEMA_DIR = pathlib.Path(__file__).resolve().parents[1] / "spec_schemas"

#: model -> (schema file, ``$defs`` key).
#:
#: Only models that genuinely mirror a spec definition. Client-side view models,
#: request builders, and shapes the spec inlines rather than naming are out of
#: scope; adding a bogus mapping would produce noise, not safety.
MODEL_TO_SCHEMA: Dict[type, Tuple[str, str]] = {
    # -- Booking lifecycle -------------------------------------------------
    wire.Booking: ("booking.json", "Booking"),
    wire.BookingPayment: ("booking.json", "BookingPayment"),
    wire.Cancellation: ("booking.json", "Cancellation"),
    wire.Dispute: ("booking.json", "Dispute"),
    wire.Action: ("booking.json", "Action"),
    wire.Buyer: ("booking.json", "Buyer"),
    wire.BuyerConsent: ("booking.json", "BuyerConsent"),
    wire.DeliveryAddress: ("booking.json", "DeliveryAddress"),
    wire.PostPaymentReturnRequest: ("booking.json", "PostPaymentReturnRequest"),
    wire.SlotReference: ("booking.json", "SlotReference"),
    # -- Availability ------------------------------------------------------
    wire.TimeSlot: ("availability.json", "TimeSlot"),
    wire.Hold: ("availability.json", "Hold"),
    wire.AvailabilitySlotBitmap: ("catalog.json", "AvailabilitySlotBitmap"),
    wire.AvailabilityHint: ("catalog.json", "AvailabilityHint"),
    platform_models.RankSignals: ("registry.json", "RankSignals"),
    # -- Catalog -----------------------------------------------------------
    wire.USPService: ("catalog.json", "Service"),
    wire.ServiceCategory: ("catalog.json", "ServiceCategory"),
    wire.ServicePolicies: ("catalog.json", "ServicePolicies"),
    wire.Pricing: ("catalog.json", "Pricing"),
    wire.Duration: ("catalog.json", "Duration"),
    wire.Channel: ("catalog.json", "Channel"),
    wire.Location: ("catalog.json", "Location"),
    wire.Provider: ("catalog.json", "Provider"),
    wire.Media: ("catalog.json", "Media"),
    wire.Rating: ("catalog.json", "Rating"),
    wire.ResourceRequirement: ("catalog.json", "ResourceRequirement"),
    wire.LocalizedFields: ("catalog.json", "LocalizedFields"),
    wire.Link: ("catalog.json", "Link"),
    # -- Shared ------------------------------------------------------------
    wire.USPMessage: ("booking.json", "Message"),
    wire.USPPaymentContext: ("booking.json", "PaymentContext"),
    # -- Profile / discovery ----------------------------------------------
    discovery.USPBusinessInfo: ("usp.json", "BusinessInfo"),
}

#: (model, property) -> why this client does not carry it.
#:
#: Every entry is a decision someone has to defend, not a way to silence the
#: test.  Prefer adding the field.
KNOWN_DIVERGENCES: Dict[Tuple[type, str], str] = {}


def _load(schema_file: str) -> dict:
    return json.loads((SCHEMA_DIR / schema_file).read_text())


def _spec_properties(schema_file: str, def_name: str) -> Set[str]:
    """Declared property names for a ``$def``, following ``allOf`` composition."""
    document = _load(schema_file)
    definition = document["$defs"][def_name]
    names: Set[str] = set(definition.get("properties", {}))
    for branch in definition.get("allOf", []):
        names |= set(branch.get("properties", {}))
    return names


def _model_wire_names(model: type[BaseModel]) -> Set[str]:
    """Every name the model accepts on the wire: field names plus aliases.

    Both matter: the models use a camelCase alias generator with
    ``populate_by_name``, so a snake_case spec property may be satisfied by
    either form.
    """
    names: Set[str] = set()
    for field_name, field in model.model_fields.items():
        names.add(field_name)
        if field.alias:
            names.add(field.alias)
        if field.validation_alias and isinstance(field.validation_alias, str):
            names.add(field.validation_alias)
    return names


def _normalize(name: str) -> str:
    """Compare ignoring case and separators, so snake_case matches camelCase."""
    return name.replace("_", "").lower()


def _ids(pairs: Iterable[Tuple[type, Tuple[str, str]]]):
    return [f"{m.__name__}<-{f}#{d}" for m, (f, d) in pairs]


@pytest.mark.parametrize(
    "model,schema_ref",
    list(MODEL_TO_SCHEMA.items()),
    ids=_ids(MODEL_TO_SCHEMA.items()),
)
def test_model_declares_every_spec_property(model, schema_ref):
    schema_file, def_name = schema_ref
    spec_names = _spec_properties(schema_file, def_name)
    model_names = {_normalize(n) for n in _model_wire_names(model)}

    missing = sorted(
        name for name in spec_names
        if _normalize(name) not in model_names
        and (model, name) not in KNOWN_DIVERGENCES
    )
    assert not missing, (
        f"{model.__name__} does not declare {missing}, which "
        f"{schema_file}#/$defs/{def_name} does.\n"
        f"The model ignores unknown members, so these are silently dropped on "
        f"parse and any code reading them gets None forever.\n"
        f"Add the field, or record a reason in KNOWN_DIVERGENCES."
    )


@pytest.mark.parametrize(
    "model,schema_ref",
    list(MODEL_TO_SCHEMA.items()),
    ids=_ids(MODEL_TO_SCHEMA.items()),
)
def test_model_declares_every_required_spec_property(model, schema_ref):
    """Required properties get the same check, stated separately.

    A missing *required* property is strictly worse than a missing optional one:
    it means the client cannot represent a conformant response at all.
    """
    schema_file, def_name = schema_ref
    definition = _load(schema_file)["$defs"][def_name]
    required = set(definition.get("required", []))
    for branch in definition.get("allOf", []):
        required |= set(branch.get("required", []))

    model_names = {_normalize(n) for n in _model_wire_names(model)}
    missing = sorted(
        name for name in required
        if _normalize(name) not in model_names
        and (model, name) not in KNOWN_DIVERGENCES
    )
    assert not missing, (
        f"{model.__name__} is missing REQUIRED {missing} from "
        f"{schema_file}#/$defs/{def_name}"
    )


def test_known_divergences_are_still_real():
    """Stop the allowlist outliving the divergence it documents.

    An entry naming a property the spec no longer has is stale, and a stale
    allowlist is how a real gap gets waved through later.
    """
    stale = []
    for (model, prop), reason in KNOWN_DIVERGENCES.items():
        schema_file, def_name = MODEL_TO_SCHEMA[model]
        if prop not in _spec_properties(schema_file, def_name):
            stale.append(f"{model.__name__}.{prop} ({reason})")
    assert not stale, f"KNOWN_DIVERGENCES entries no longer in the spec: {stale}"


def test_every_mapped_schema_def_exists():
    """A typo in MODEL_TO_SCHEMA would otherwise skip a model silently."""
    for model, (schema_file, def_name) in MODEL_TO_SCHEMA.items():
        document = _load(schema_file)
        assert def_name in document.get("$defs", {}), (
            f"{schema_file} has no $defs/{def_name} (mapped from {model.__name__})"
        )


def test_vendored_schemas_record_their_provenance():
    """The pin is the whole basis for trusting these files."""
    source = (SCHEMA_DIR / "SOURCE").read_text()
    assert "commit:" in source and "usp_version:" in source
    # A bare 40-char sha, not a branch name that moves under us.
    commit = next(l.split(":", 1)[1].strip() for l in source.splitlines() if l.startswith("commit:"))
    assert len(commit) == 40, f"expected a pinned commit sha, got {commit!r}"


def test_the_revision_regression_specifically():
    """The bug this whole file exists because of.

    Kept as a named test rather than trusting the parametrized sweep, because
    this one field is load-bearing for conditional writes and the failure was
    invisible in every other way.
    """
    assert "revision" in wire.Booking.model_fields
    parsed = wire.Booking.model_validate({
        "id": "b1", "service_id": "s1", "service_name": "n",
        "slot": {"id": "sl", "service_id": "s1", "start": "2026-01-01T10:00:00Z",
                 "end": "2026-01-01T11:00:00Z", "duration": "PT1H", "state": "available"},
        "buyer": {"first_name": "A", "last_name": "B", "email": "a@b.c"},
        "party_size": 1, "status": "confirmed", "confirmation_mode": "auto",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "revision": "rev-42",
    })
    assert parsed.revision == "rev-42", (
        "the business's concurrency token was dropped on parse, so If-Match "
        "would never be sent and conditional writes would be silently disabled"
    )


def test_structured_and_summary_only_availability_hints_parse():
    summary_only = wire.AvailabilityHint.model_validate({
        "summary": "Availability varies",
        "generated_at": "2026-08-20T10:00:00Z",
    })
    assert summary_only.slot_bitmaps is None

    structured = wire.AvailabilityHint.model_validate({
        "summary": "Openings this week",
        "generated_at": "2026-08-20T10:00:00Z",
        "valid_until": "2026-08-20T10:15:00Z",
        "next_available_date": "2026-08-21",
        "slot_bitmaps": [{
            "duration": "PT60M",
            "starts_at": "2026-08-21T09:00:00Z",
            "start_interval": "PT15M",
            "slot_count": 16,
            "encoding": "roaring32-portable-base64",
            "bitmap": "OjAAAAEAAAAAAAMAEAAAAAEACAA=",
        }],
    })
    assert structured.slot_bitmaps
    assert structured.slot_bitmaps[0].slot_count == 16


def test_availability_hint_rejects_present_empty_bitmaps():
    with pytest.raises(ValueError):
        wire.AvailabilityHint.model_validate({
            "summary": "Unknown",
            "generated_at": "2026-08-20T10:00:00Z",
            "slot_bitmaps": [],
        })


def test_rank_signals_require_all_five_members_and_preserve_null_vs_zero():
    with pytest.raises(ValueError):
        platform_models.RankSignals.model_validate({
            "relevance": 0.8,
            "coverage": None,
            "density": None,
            "hint_usable": False,
        })

    unknown = platform_models.RankSignals.model_validate({
        "relevance": 0.8,
        "coverage": None,
        "density": None,
        "soonness": None,
        "hint_usable": False,
    })
    known_empty = platform_models.RankSignals.model_validate({
        "relevance": 0.8,
        "coverage": 0,
        "density": 0,
        "soonness": 0,
        "hint_usable": True,
    })
    assert unknown.coverage is None
    assert known_empty.coverage == 0


def test_malformed_search_hit_hint_is_dropped_without_dropping_hit():
    hit = platform_models.ServiceSearchResult.model_validate({
        "service_id": "service-1",
        "service_name": "Service",
        "business": {
            "id": "business-1",
            "name": "Business",
            "profile_url": "https://example.com/.well-known/usp",
        },
        "category": "Wellness",
        "availability_hint": {
            "summary": "Bad structured data",
            "generated_at": "2026-08-20T10:00:00Z",
            "slot_bitmaps": [],
        },
    })
    assert hit.service_id == "service-1"
    assert hit.availability_hint is None
