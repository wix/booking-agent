"""Platform-side USP orchestration models for booking-agent-cli.

These models live on the platform (Booking Agent CLI) side and are NOT part of the
USP wire protocol. Business-side implementations do not need them.
"""
from enum import Enum
from typing import Any, List, Optional, Union

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel

from .wire import AvailabilityHint, Coordinates, RegistryBusinessInfo


class BookingPaymentMode(str, Enum):
    GENERIC = "generic"
    UCP = "ucp"
    REDIRECT = "redirect"


class ServiceSearchLocation(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    coordinates: Coordinates
    radius_km: Optional[float] = None


class RangeMatchMode(str, Enum):
    """How a service interval S is compared to a registry filter interval F.

    Mirrors ``schemas/registry.json#/$defs/RangeMatchMode`` (USP §6.3.1).
    Omitted ``match`` on the wire MUST be treated as ``overlap`` by registries.
    """

    OVERLAP = "overlap"
    CONTAINED = "contained"
    CONTAINS = "contains"
    EQUALS = "equals"


class PriceRange(BaseModel):
    """Registry ``price_range`` filter (``schemas/registry.json#/$defs/PriceRangeFilter``)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    min: Optional[int] = None
    max: Optional[int] = None
    currency: str = "USD"
    match: Optional[RangeMatchMode] = None


class DurationRange(BaseModel):
    """Registry ``duration_range`` filter (``schemas/registry.json#/$defs/DurationRangeFilter``)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    min_minutes: Optional[int] = None
    max_minutes: Optional[int] = None
    match: Optional[RangeMatchMode] = None


class DesiredServiceTimeMoment(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    at: str


class DesiredServiceTimeRange(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    start: str
    end: str


class DesiredServiceTimeFrom(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    start: str


DesiredServiceTimePreference = Union[
    DesiredServiceTimeMoment,
    DesiredServiceTimeRange,
    DesiredServiceTimeFrom,
]


class ServiceSearchIntent(BaseModel):
    """Platform-side intent object for service discovery (analogous to IntentMandate)."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    natural_language_description: str
    location: Optional[ServiceSearchLocation] = None
    verticals: List[str] = []
    categories: List[str] = []
    query: str
    price_range: Optional[PriceRange] = None
    duration_range: Optional[DurationRange] = None
    date_preference: Optional[str] = None
    desired_service_time_ranges: Optional[List[DesiredServiceTimePreference]] = None
    prefer_sooner_availability_slots: Optional[bool] = None
    intent_expiry: str


# ---------------------------------------------------------------------------
# Registry search response models (mirrors /registry/search_services response)
# ---------------------------------------------------------------------------

class ServiceSearchResultPrice(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    model: Optional[str] = None
    amount: Optional[int] = None
    currency: Optional[str] = None

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, value: object) -> Optional[int]:
        if value is None or value == "":
            return None
        return int(value)


class ServiceSearchResultLocation(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    address: Optional[str] = None
    coordinates: Optional[Coordinates] = None


class RankSignals(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    relevance: float = Field(ge=0, le=1)
    coverage: Optional[float] = Field(ge=0, le=1)
    density: Optional[float] = Field(ge=0, le=1)
    soonness: Optional[float] = Field(ge=0, le=1)
    hint_usable: bool


class ServiceSearchResult(BaseModel):
    """schemas/registry.json#/$defs/ServiceSearchResult.

    Spec REQUIRES (service_id, service_name, business, category, pricing, timezone).
    ``pricing`` (named ``price`` here, the field name the client reads) and
    ``timezone`` are Optional: the client accepts results without them.
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    service_id: str
    service_name: str
    business: RegistryBusinessInfo
    category: str
    duration_minutes: Optional[int] = None
    price: Optional[ServiceSearchResultPrice] = Field(
        default=None,
        validation_alias=AliasChoices("price", "pricing"),
        serialization_alias="pricing",
    )
    location: Optional[ServiceSearchResultLocation] = None
    timezone: Optional[str] = None
    category_ids: Optional[List[str]] = None
    availability_hint: Optional[AvailabilityHint] = None
    rank_signals: Optional[RankSignals] = None

    @field_validator("availability_hint", mode="before")
    @classmethod
    def _drop_malformed_availability_hint(
        cls, value: Any
    ) -> Optional[AvailabilityHint]:
        if value is None or isinstance(value, AvailabilityHint):
            return value
        try:
            return AvailabilityHint.model_validate(value)
        except (ValidationError, TypeError, ValueError):
            return None


class ServiceSearchPagination(BaseModel):
    """Pagination metadata; supports cursor- and offset-based styles."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    cursor: Optional[str] = None
    has_more: Optional[bool] = None
    count: Optional[int] = None
    offset: Optional[int] = None
    has_next: Optional[bool] = None
