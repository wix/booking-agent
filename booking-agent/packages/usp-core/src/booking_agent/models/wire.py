"""Shared USP protocol domain models.

These Pydantic models mirror the USP JSON schemas in universal-scheduling-protocol
(`schemas/catalog.json`, `schemas/availability.json`, `schemas/booking.json`) and
represent the wire format for the Universal Scheduling Protocol.
Both the platform side (shopping_agent) and the business side (wix_commerce_agent,
when USP support is added) import from here.
"""
from enum import Enum

from pydantic.alias_generators import to_camel
from typing import Optional, List, Dict, Any, Literal, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Shared enums (booking + catalog payment timing)
# ---------------------------------------------------------------------------


class ConfirmationMode(str, Enum):
    """booking.json / catalog ServicePolicies.confirmation_mode."""

    AUTO = "auto"
    MANUAL = "manual"


class BookingPaymentStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    DEPOSIT_PAID = "deposit_paid"
    PAID = "paid"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class BookingPaymentTiming(str, Enum):
    AT_BOOKING = "at_booking"
    AT_SERVICE = "at_service"
    DEPOSIT_REQUIRED = "deposit_required"


# ---------------------------------------------------------------------------
# Catalog models (mirrors schemas/catalog.json)
# ---------------------------------------------------------------------------

class Coordinates(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    lat: float
    lng: float


class Location(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    coordinates: Optional[Coordinates] = None


class DurationRange(BaseModel):
    """catalog.json Duration.range — min/max/step ISO 8601 durations."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    min: Optional[str] = None
    max: Optional[str] = None
    step: Optional[str] = None


class Duration(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    fixed: Optional[str] = None
    range: Optional[DurationRange] = None
    undetermined: Optional[bool] = None
    buffer_before: Optional[str] = None
    buffer_after: Optional[str] = None


class PricingModel(str, Enum):
    FIXED = "fixed"
    HOURLY = "hourly"
    PER_PERSON = "per_person"
    VARIABLE = "variable"
    FREE = "free"


class PricingPriceRange(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    min: int
    max: int


class PricingDeposit(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    value: Union[int, float]
    refundable: Optional[bool] = None


class Pricing(BaseModel):
    """Pricing per catalog.json $defs/Pricing.

    Spec invariants:
        - ``amount`` is REQUIRED when ``model`` is ``fixed``, ``hourly`` or ``per_person``,
          and MUST NOT be present when ``model`` is ``free``.
        - ``price_range`` MUST NOT be present when ``model`` is ``free``.
        - ``deposit`` is REQUIRED when payment_timing is ``deposit_required``.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    model: Union[PricingModel, str]
    amount: Optional[int] = None
    currency: str
    price_range: Optional[PricingPriceRange] = None
    deposit: Optional[PricingDeposit] = None


class ChannelType(str, Enum):
    """Catalog channel.type (schemas/catalog.json).

    ``in_person`` was renamed to ``at_business_location`` in USP #40; treat the
    old value as unrecognized, not as a synonym.
    """

    AT_BUSINESS_LOCATION = "at_business_location"
    AT_BUYER_LOCATION = "at_buyer_location"
    VIRTUAL = "virtual"
    PHONE = "phone"
    HYBRID = "hybrid"


class ResourceKind(str, Enum):
    STAFF = "staff"
    ROOM = "room"
    EQUIPMENT = "equipment"
    OTHER = "other"


class ResourceOption(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None


class ResourceRequirement(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: Union[ResourceKind, str]
    name: Optional[str] = None
    selectable: bool = False
    options: Optional[List[ResourceOption]] = None


class Channel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: Union[ChannelType, str]
    virtual_provider: Optional[str] = None
    instructions: Optional[str] = None
    service_area: Optional[str] = None


class CancellationPolicy(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    allowed: bool
    free_cancellation_until: Optional[str] = None
    late_cancellation_fee: Optional[int] = None
    no_cancellation_after: Optional[str] = None


class ReschedulingPolicy(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    allowed: bool
    free_reschedule_until: Optional[str] = None
    max_reschedules: Optional[int] = None
    fee: Optional[int] = None


class NoShowPolicy(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    fee: Optional[int] = None
    fee_percentage: Optional[int] = None
    grace_period: Optional[str] = None


class BookingWindow(BaseModel):
    """schemas/catalog.json#/$defs/ServicePolicies.booking_window.

    Spec REQUIRES all three of (min_advance, max_advance, slot_interval), but the
    Wix Bookings adapter currently omits ``slot_interval`` on many services
    (and sometimes omits the whole ``booking_window``). To keep parsing real
    business responses, ``slot_interval`` is kept Optional here — a deliberate
    permissive divergence from the spec.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    min_advance: str
    max_advance: str
    slot_interval: Optional[str] = None


class ServicePolicies(BaseModel):
    """schemas/catalog.json#/$defs/ServicePolicies.

    Spec REQUIRES (cancellation, rescheduling, booking_window, confirmation_mode,
    requires_payment). ``booking_window`` is kept Optional here because the Wix
    Bookings adapter does not populate it for many services — deliberate
    permissive divergence (see BookingWindow docstring).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    cancellation: CancellationPolicy
    rescheduling: ReschedulingPolicy
    no_show: Optional[NoShowPolicy] = None
    booking_window: Optional[BookingWindow] = None
    confirmation_mode: Union[ConfirmationMode, str]
    requires_payment: bool
    payment_timing: Optional[Union[BookingPaymentTiming, str]] = None


class ServiceImage(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    url: str
    alt: Optional[str] = None
    type: Optional[str] = None


class AvailabilitySlotBitmap(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    duration: str
    starts_at: str
    start_interval: str
    slot_count: int = Field(ge=1, le=4294967296)
    encoding: Literal["roaring32-portable-base64"]
    bitmap: str


class AvailabilityHint(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    summary: str
    generated_at: str
    valid_until: Optional[str] = None
    next_available_date: Optional[str] = None
    slot_bitmaps: Optional[List[AvailabilitySlotBitmap]] = Field(
        default=None, min_length=1
    )


class ServiceCapacity(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    min: Optional[int] = None
    max: Optional[int] = None
    waitlist: Optional[bool] = None


class ServiceCategory(BaseModel):
    """A category label from a taxonomy system (catalog.json ServiceCategory).

    ``taxonomy`` defaults to ``merchant`` for the common single-category case.
    Each entry MUST carry at least one of ``id``, ``name``, or ``value``.
    External (non-``merchant``) taxonomies MUST carry ``value``. Exactly one
    entry in ``categories`` is primary (see :func:`primary_service_category`).
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    taxonomy: str = "merchant"
    id: Optional[str] = None
    name: Optional[str] = None
    parent_id: Optional[str] = None
    value: Optional[str] = None
    primary: Optional[bool] = None

    @model_validator(mode="after")
    def _require_identity_fields(self) -> "ServiceCategory":
        if not (self.id or self.name or self.value):
            raise ValueError(
                "ServiceCategory MUST carry at least one of id, name, or value"
            )
        if self.taxonomy != "merchant" and not self.value:
            raise ValueError(
                "External (non-merchant) ServiceCategory taxonomies MUST carry value"
            )
        return self


# Backward-compatible alias for the pre-#54 thin {value, taxonomy} entry type.
ServiceCategoryEntry = ServiceCategory


def primary_service_category(
    categories: Optional[List["ServiceCategory"]],
) -> Optional["ServiceCategory"]:
    """Return the canonical primary entry from ``categories[]`` (USP category rules).

    1. Exactly one ``primary: true`` wins.
    2. Else if no entry sets ``primary`` and exactly one has ``taxonomy: merchant``,
       that merchant entry is primary.
    3. Else the first entry in array order is primary.
    """
    if not categories:
        return None
    flagged = [c for c in categories if c.primary is True]
    if len(flagged) == 1:
        return flagged[0]
    unset = all(c.primary is not True for c in categories)
    if unset:
        merchants = [c for c in categories if c.taxonomy == "merchant"]
        if len(merchants) == 1:
            return merchants[0]
    return categories[0]


def project_category_string(
    *,
    categories: Optional[List["ServiceCategory"]] = None,
    service_type: Optional[str] = None,
) -> str:
    """Project ``categories[]`` to the flat registry ``ServiceSearchResult.category`` string.

    Pick order: primary ``name``, else primary ``value``, else primary ``id``,
    else first entry ``value``, else ``service_type``, else ``\"\"``.
    """
    primary = primary_service_category(categories)
    if primary is not None:
        if primary.name:
            return primary.name
        if primary.value:
            return primary.value
        if primary.id:
            return primary.id
    if categories:
        first = categories[0]
        if first.value:
            return first.value
    return service_type or ""


class RegistryBusinessInfo(BaseModel):
    """Business info embedded in registry search results.

    Aligns with schemas/registry.json#/$defs/RegistryBusinessRef which requires
    (id, profile_url, deployment_mode, name). The Wix semantic-search endpoint
    still emits the legacy field name ``usp_profile_url`` (and frequently omits
    ``deployment_mode``), so this model accepts BOTH names via the AliasChoices
    construct and keeps ``deployment_mode`` Optional with the spec-recommended
    default of ``"standalone"``.
    """
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="ignore"
    )

    id: str
    name: str
    profile_url: str = Field(
        validation_alias=AliasChoices("profile_url", "profileUrl", "usp_profile_url", "uspProfileUrl"),
        serialization_alias="profileUrl",
    )
    deployment_mode: Optional[str] = "standalone"

    @property
    def usp_profile_url(self) -> str:
        """Backwards-compatible accessor for the pre-rename field name."""
        return self.profile_url


class Link(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    url: str
    title: Optional[str] = None


class Provider(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: str
    url: Optional[str] = None
    links: Optional[List[Link]] = None


class Media(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    url: str
    alt_text: Optional[str] = None
    role: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class Rating(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    value: float
    scale_max: float
    scale_min: float = 1
    count: Optional[int] = None


class DescriptionContent(BaseModel):
    """Structured service description (catalog.json Description)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    plain: str
    markdown: Optional[str] = None
    html: Optional[str] = None


class ServiceLifecycleStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class LocalizedFields(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    name: Optional[Dict[str, str]] = None
    description: Optional[Dict[str, str]] = None
    category_name: Optional[Dict[str, str]] = None
    channel_instructions: Optional[Dict[str, str]] = None


def _normalize_category_entry(raw: Any) -> Any:
    """Normalize a category value for ServiceCategory (default taxonomy=merchant)."""
    if isinstance(raw, ServiceCategory):
        return raw
    if not isinstance(raw, dict):
        return raw
    entry = dict(raw)
    if not entry.get("taxonomy"):
        entry["taxonomy"] = "merchant"
    return entry


class USPService(BaseModel):
    """A bookable offering from a business (mirrors catalog.json Service).

    Post issue #54 the singular ``category`` field is removed from the wire
    schema; enriched ``categories[]`` is canonical. Legacy responses that still
    send ``category`` are coerced into a one-element ``categories`` array.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    business_id: str
    provider: Optional[Provider] = None
    name: str
    description: Optional[Union[str, DescriptionContent]] = None
    type: str
    categories: Optional[List[ServiceCategory]] = None
    duration: Duration
    pricing: Pricing
    locations: Optional[List[Location]] = None
    resources: Optional[List[ResourceRequirement]] = None
    channel: Channel
    policies: ServicePolicies
    capacity: Optional[ServiceCapacity] = None
    media: Optional[List[Media]] = None
    images: Optional[List[ServiceImage]] = None
    rating: Optional[Rating] = None
    status: Optional[Union[ServiceLifecycleStatus, str]] = None
    handle: Optional[str] = None
    url: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    availability_hint: Optional[AvailabilityHint] = None
    links: Optional[List[Link]] = None
    localized: Optional[LocalizedFields] = None
    business: Optional[RegistryBusinessInfo] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_category(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        categories = payload.get("categories")
        legacy = payload.pop("category", None)
        if categories is None and legacy is not None:
            if isinstance(legacy, list):
                payload["categories"] = [
                    _normalize_category_entry(item) for item in legacy
                ]
            else:
                payload["categories"] = [_normalize_category_entry(legacy)]
        elif isinstance(categories, list):
            payload["categories"] = [
                _normalize_category_entry(item) for item in categories
            ]
        return payload

    @property
    def category(self) -> Optional[ServiceCategory]:
        """Primary category entry (compat for pre-#54 callers)."""
        return primary_service_category(self.categories)


# ---------------------------------------------------------------------------
# Availability models (mirrors schemas/availability.json)
# ---------------------------------------------------------------------------

class SlotCapacity(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    total: Optional[int] = None
    remaining: Optional[int] = None
    held: Optional[int] = None


class SlotResource(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    type: Optional[str] = None
    name: Optional[str] = None


class SlotLocation(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: Optional[str] = None
    name: Optional[str] = None


class SlotPricing(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    amount: Optional[int] = None
    currency: Optional[str] = None
    label: Optional[str] = None


class SlotState(str, Enum):
    AVAILABLE = "available"
    LIMITED = "limited"
    WAITLIST = "waitlist"


class TimeSlot(BaseModel):
    """A specific, bookable time window for a service (mirrors availability.json TimeSlot)."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    service_id: str
    start: str
    end: str
    duration: str
    state: Union[SlotState, str]
    capacity: Optional[SlotCapacity] = None
    resources: Optional[List[SlotResource]] = None
    location: Optional[SlotLocation] = None
    pricing: Optional[SlotPricing] = None


class AvailabilityResult(List[TimeSlot]):
    """Slots from ``POST /availability/query`` plus the response's zone fields (USP 4.3.1).

    A plain list of slots, so callers that only iterate keep working.
    ``business_timezone`` names the business's clock. ``applied_date_bounds_timezone``
    only says how date-only bounds were read and must not name that clock.
    """

    def __init__(
        self,
        slots: Any = (),
        *,
        business_timezone: Optional[str] = None,
        applied_date_bounds_timezone: Optional[str] = None,
    ) -> None:
        super().__init__(slots)
        self.business_timezone = business_timezone
        self.applied_date_bounds_timezone = applied_date_bounds_timezone


class HoldStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"
    CONVERTED = "converted"


class Hold(BaseModel):
    """A temporary reservation of a time slot (mirrors availability.json Hold)."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    slot_id: str
    service_id: str
    spots: int = 1
    expires_at: str
    status: HoldStatus


# ---------------------------------------------------------------------------
# Booking models (mirrors schemas/booking.json)
# ---------------------------------------------------------------------------

class SlotReference(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    start: str
    end: str
    duration: str


class BuyerConsent(BaseModel):
    """Declarative consent signals (booking.json $defs/BuyerConsent).

    Nested inside the Buyer object per UCP convention. Boolean values are the
    common consent categories; vendor-namespaced keys (e.g. ``x-acme-research``)
    are accepted via Pydantic's extra-field handling on the parent Buyer.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    analytics: Optional[bool] = None
    marketing: Optional[bool] = None
    preferences: Optional[bool] = None
    sale_of_data: Optional[bool] = None
    health_data: Optional[bool] = None


class Buyer(BaseModel):
    """Per booking.json $defs/Buyer."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    first_name: str
    last_name: str
    email: str
    phone_number: Optional[str] = None
    consent: Optional[BuyerConsent] = None


class BookingPayment(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: BookingPaymentStatus
    timing: BookingPaymentTiming
    amount: Optional[int] = None
    currency: Optional[str] = None
    amount_due: Optional[int] = None
    deposit_amount: Optional[int] = None
    tax_amount: Optional[int] = None
    transaction_id: Optional[str] = None
    order_reference: Optional[str] = None


class LineItem(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    label: str
    amount: int
    quantity: int
    item_id: str


class PaymentContextMetadata(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    booking_id: str
    service_id: str
    service_type: str
    slot_start: Optional[str] = None


class USPPaymentContext(BaseModel):
    """Universal payment handoff object from the business to the platform."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    amount_due: int
    currency: str
    description: str
    line_items: List[LineItem]
    tax_amount: Optional[int] = None
    metadata: PaymentContextMetadata


class PostPaymentReturnRequest(BaseModel):
    """Return redirect specification sent in POST /bookings for the redirect checkout path.

    The business MUST redirect the buyer to `url` with all `params` entries appended
    verbatim as query parameters — both when payment completes successfully AND when
    the buyer cancels or abandons payment. The platform uses `params` as opaque
    correlation state (analogous to OAuth 2.0 `redirect_uri` + `state`) to identify
    the pending session without knowing the booking_id in advance.
    """
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    url: str
    params: Optional[Dict[str, str]] = None


class USPPaymentResult(BaseModel):
    """Payment result sent from the platform back to the business."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: str
    provider: Optional[str] = None
    transaction_id: str
    amount_paid: int
    currency: str
    order_reference: Optional[str] = None


class MessageType(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class MessageSeverity(str, Enum):
    REQUIRES_BUYER_INPUT = "requires_buyer_input"
    RECOVERABLE = "recoverable"
    REQUIRES_BUYER_REVIEW = "requires_buyer_review"


class USPMessage(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: MessageType
    code: str
    content: str
    severity: Optional[MessageSeverity] = None
    path: Optional[str] = None


class ActionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    EXPIRED = "expired"


class Action(BaseModel):
    """A pending task the buyer must complete before a booking can be confirmed."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    type: str
    status: ActionStatus
    continue_url: str
    expires_at: str
    message: Optional[USPMessage] = None
    payment_context: Optional[USPPaymentContext] = None


class Cancellation(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    reason: Optional[str] = None
    canceled_by: Optional[str] = None
    fee: Optional[int] = None
    refund_amount: Optional[int] = None
    canceled_at: Optional[str] = None


class BookingStatus(str, Enum):
    PENDING = "pending"
    REQUIRES_ACTION = "requires_action"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    CANCELED = "canceled"


class BookingResource(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    type: Optional[str] = None
    name: Optional[str] = None


class BookingLocation(BaseModel):
    """Business location reference for at_business_location / hybrid bookings."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: Optional[str] = None
    name: Optional[str] = None


class DeliveryAddress(BaseModel):
    """Buyer's service delivery address (booking.json $defs/DeliveryAddress).

    REQUIRED on create-booking when the service's channel.type is
    ``at_buyer_location``; echoed back on the Booking object.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    line1: str
    city: str
    country: str
    line2: Optional[str] = None
    region: Optional[str] = None
    postal_code: Optional[str] = None
    coordinates: Optional[Coordinates] = None


class DisputeStatus(str, Enum):
    OPENED = "opened"
    UNDER_REVIEW = "under_review"
    RESOLVED_BUYER = "resolved_buyer"
    RESOLVED_BUSINESS = "resolved_business"


class Dispute(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: DisputeStatus
    reason: str
    opened_at: str
    resolved_at: Optional[str] = None


class Booking(BaseModel):
    """A scheduled service instance (mirrors booking.json Booking)."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    service_id: str
    service_name: str
    slot: SlotReference
    buyer: Buyer
    recipient: Optional[Buyer] = None
    party_size: int
    resources: Optional[List[BookingResource]] = None
    location: Optional[BookingLocation] = None
    delivery_address: Optional[DeliveryAddress] = None
    status: BookingStatus
    confirmation_mode: Union[ConfirmationMode, str]
    payment: Optional[BookingPayment] = None
    actions: Optional[List[Action]] = None
    notes: Optional[str] = None
    booking_url: Optional[str] = None
    dispute: Optional[Dispute] = None
    messages: Optional[List[USPMessage]] = None
    cancellation: Optional[Cancellation] = None
    created_at: str
    updated_at: str
    expires_at: Optional[str] = None
    #: Opaque concurrency token (specification.md Section 5.6).  Its *presence* is the
    #: advertisement that the business supports conditional writes: echo it back
    #: as ``If-Match`` on mutating calls and the business rejects a stale one
    #: with 412 rather than clobbering a concurrent change.  Absent means
    #: unconditional writes, exactly like a missing HTTP ETag.
    revision: Optional[str] = None
