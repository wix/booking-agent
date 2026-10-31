"""Booking questions extension wire models (mirrors schemas/booking_questions.json).

The business stays authoritative for validation, so these models accept what a
conformant business sends and keep every declared member. Shape rules a JSON
Schema cannot express (earlier-question references, unique option values) live
in ``booking_agent.booking_questions``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag
from typing_extensions import Annotated

AnswerValue = Union[bool, int, float, str, List[str]]
BookingQuestionAssessmentToken = str


class _Model(BaseModel):
    """Members keep their wire names; the few camelCase ones carry explicit aliases."""

    model_config = ConfigDict(populate_by_name=True)

    def to_wire(self) -> Dict[str, Any]:
        return self.model_dump(by_alias=True, exclude_unset=True, mode="json")


class BookingQuestionLabel(_Model):
    """catalog.json Description with a bounded ``plain``; also used for text and notices."""

    plain: str
    markdown: Optional[str] = None
    html: Optional[str] = None


class BookingQuestionConditionLeaf(_Model):
    question: str
    op: Literal["equals", "in", "contains", "answered", "gt", "gte", "lt", "lte"]
    value: Optional[Any] = None


class BookingQuestionConditionGroup(_Model):
    """A combinator: exactly one of ``all``, ``any`` or ``not``."""

    all: Optional[List["BookingQuestionCondition"]] = None
    any: Optional[List["BookingQuestionCondition"]] = None
    not_: Optional["BookingQuestionCondition"] = Field(default=None, alias="not")


def _condition_kind(value: Any) -> str:
    raw = value if isinstance(value, dict) else getattr(value, "__dict__", {})
    return "leaf" if "question" in raw else "group"


BookingQuestionCondition = Annotated[
    Union[
        Annotated[BookingQuestionConditionLeaf, Tag("leaf")],
        Annotated[BookingQuestionConditionGroup, Tag("group")],
    ],
    Discriminator(_condition_kind),
]
BookingQuestionConditionGroup.model_rebuild()


class BookingQuestionOptionLocalized(_Model):
    title: Optional[str] = None


class BookingQuestionOption(_Model):
    const: str
    title: str
    image_url: Optional[str] = None
    exclusive: Optional[bool] = None
    localized: Optional[Dict[str, BookingQuestionOptionLocalized]] = None


class BookingQuestionStringSchema(_Model):
    type: Literal["string"]
    min_length: Optional[int] = Field(default=None, alias="minLength")
    max_length: Optional[int] = Field(default=None, alias="maxLength")
    format: Optional[Literal["email", "uri", "date", "time", "date-time", "phone"]] = None
    allowed_country_codes: Optional[List[str]] = None
    not_before: Optional[str] = None
    not_after: Optional[str] = None


class BookingQuestionNumberSchema(_Model):
    type: Literal["number", "integer"]
    minimum: Optional[Union[int, float]] = None
    maximum: Optional[Union[int, float]] = None


class BookingQuestionBooleanSchema(_Model):
    type: Literal["boolean"]
    const: Optional[bool] = None


class BookingQuestionChoiceItems(_Model):
    any_of: List[BookingQuestionOption] = Field(alias="anyOf")


class BookingQuestionSingleChoiceSchema(_Model):
    type: Literal["string"]
    one_of: List[BookingQuestionOption] = Field(alias="oneOf")


class BookingQuestionMultipleChoiceSchema(_Model):
    type: Literal["array"]
    items: BookingQuestionChoiceItems
    min_items: Optional[int] = Field(default=None, alias="minItems")
    max_items: Optional[int] = Field(default=None, alias="maxItems")
    unique_items: bool = Field(default=True, alias="uniqueItems")


def _answer_schema_kind(value: Any) -> str:
    raw = value if isinstance(value, dict) else getattr(value, "__dict__", {})
    kind = raw.get("type")
    if kind == "array":
        return "multiple"
    if kind == "boolean":
        return "boolean"
    if kind in ("number", "integer"):
        return "number"
    if "oneOf" in raw or "one_of" in raw:
        return "single"
    return "string"


BookingQuestionAnswerSchema = Annotated[
    Union[
        Annotated[BookingQuestionSingleChoiceSchema, Tag("single")],
        Annotated[BookingQuestionMultipleChoiceSchema, Tag("multiple")],
        Annotated[BookingQuestionBooleanSchema, Tag("boolean")],
        Annotated[BookingQuestionNumberSchema, Tag("number")],
        Annotated[BookingQuestionStringSchema, Tag("string")],
    ],
    Discriminator(_answer_schema_kind),
]


class BookingQuestionItemLocalized(_Model):
    label: Optional[BookingQuestionLabel] = None
    title: Optional[BookingQuestionLabel] = None
    description: Optional[BookingQuestionLabel] = None
    content: Optional[BookingQuestionLabel] = None
    placeholder: Optional[str] = None


class BookingQuestionSection(_Model):
    kind: Literal["section"]
    id: str
    title: BookingQuestionLabel
    description: Optional[BookingQuestionLabel] = None
    when: Optional[BookingQuestionCondition] = None
    localized: Optional[Dict[str, BookingQuestionItemLocalized]] = None


class BookingQuestionNotice(_Model):
    kind: Literal["notice"]
    id: str
    content: BookingQuestionLabel
    presentation: Literal["notice", "disclosure"] = "notice"
    when: Optional[BookingQuestionCondition] = None
    localized: Optional[Dict[str, BookingQuestionItemLocalized]] = None


class BookingQuestion(_Model):
    kind: Literal["question"]
    id: str
    label: BookingQuestionLabel
    description: Optional[BookingQuestionLabel] = None
    placeholder: Optional[str] = None
    rendering: Optional[
        Literal["text", "textarea", "rating", "radio", "dropdown", "checkbox", "tags", "image_choice"]
    ] = None
    required: Union[bool, BookingQuestionCondition]
    when: Optional[BookingQuestionCondition] = None
    collection: Literal["in_band", "platform_surface", "business_surface"] = "in_band"
    sensitive: bool = False
    default: Optional[AnswerValue] = None
    maps_to: Optional[Literal["notes"]] = None
    consent_purpose: Optional[Literal["marketing"]] = None
    consent_channel: Optional[
        Literal["email", "sms", "phone", "post", "messaging", "other"]
    ] = None
    answer_schema: BookingQuestionAnswerSchema = Field(alias="schema")
    localized: Optional[Dict[str, BookingQuestionItemLocalized]] = None


class BookingQuestionExternalTask(_Model):
    kind: Literal["external_task"]
    id: str
    label: BookingQuestionLabel
    description: Optional[BookingQuestionLabel] = None
    task_type: Literal["file", "signature", "markup"]
    required: Union[bool, BookingQuestionCondition]
    when: Optional[BookingQuestionCondition] = None
    collection: Literal["business_surface"]
    localized: Optional[Dict[str, BookingQuestionItemLocalized]] = None


BookingQuestionItem = Annotated[
    Union[BookingQuestionSection, BookingQuestionNotice, BookingQuestion, BookingQuestionExternalTask],
    Field(discriminator="kind"),
]


class BookingQuestionSetLocalized(_Model):
    title: Optional[BookingQuestionLabel] = None
    description: Optional[BookingQuestionLabel] = None


class BookingQuestionSet(_Model):
    id: str
    revision: str
    title: Optional[BookingQuestionLabel] = None
    description: Optional[BookingQuestionLabel] = None
    language: Optional[str] = None
    localized: Optional[Dict[str, BookingQuestionSetLocalized]] = None
    purpose: Literal["eligibility", "configuration", "intake"]
    completion_timing: Literal["preflight", "before_confirmation", "before_service"]
    completion_requirement: Literal["required", "optional"] = "required"
    frequency: Literal["every_booking", "once_per_buyer", "once_per_buyer_per_service"]
    validity: Optional[str] = None
    subject: Literal["buyer", "recipient"] = "buyer"
    due_at: Optional[str] = None
    items: List[BookingQuestionItem]


RequiredBuyerField = Literal["first_name", "last_name", "email", "phone_number"]


class BookingQuestionSubmission(_Model):
    set_id: str
    revision: str
    answers: Dict[str, AnswerValue]
    collected_via: Optional[Dict[str, Literal["conversation", "platform_surface"]]] = None
    action_id: Optional[str] = None


class BookingQuestionSetRef(_Model):
    set_id: str
    revision: str


BookingQuestionStatus = Literal[
    "pending", "completed", "expired", "waived", "extended", "under_review", "rejected",
]


class BookingQuestionState(_Model):
    set_id: str
    revision: str
    status: BookingQuestionStatus
    completion_timing: Literal["preflight", "before_confirmation", "before_service"]
    due_at: Optional[str] = None
    valid_until: Optional[str] = None
    message: Optional[str] = None
    continue_url: Optional[str] = None
    answers: Optional[Dict[str, AnswerValue]] = None


class BookingEligibilityCheckRequest(_Model):
    buyer: Optional[Dict[str, Any]] = None
    recipient: Optional[Dict[str, Any]] = None
    booking_question_submissions: Optional[List[BookingQuestionSubmission]] = None


class BookingEligibilityCheckResult(_Model):
    decision: Literal["eligible", "ineligible", "requires_input"]
    required_sets: Optional[List[BookingQuestionSetRef]] = None
    assessment_token: Optional[str] = None
    expires_at: Optional[str] = None


class BookingQuestionsSummary(_Model):
    set_count: int
    has_preflight_eligibility: bool
    needs_platform_surface: bool
    needs_business_surface: bool
    has_before_service_requirements: bool


class BuyerInputActionConfig(_Model):
    mode: Literal["collect_answers", "acknowledge"]
    sets: List[BookingQuestionSetRef]
    item_ids: Optional[List[str]] = None
    presentation: Literal["verbatim"] = "verbatim"


class BuyerInputAction(_Model):
    id: str
    config: BuyerInputActionConfig
    continue_url: Optional[str] = None
