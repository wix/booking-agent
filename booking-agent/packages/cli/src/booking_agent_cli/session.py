"""``.usp-session.json`` schema, persistence, and read helpers.

The *store* is the single source of truth for the flow engine.  Only
``session.py`` writes the file.  The engine reads via these accessors and
calls ``save_session`` / ``save_store`` to persist transitions.

File format (schema_version 2):
  {
    "schema_version": 2,
    "max_sessions": 30,
    "sessions_created": <int, never decrements>,
    "active_session_id": "<uuid>",
    "sessions": [ { "id": "...", "created_at": "...", "user_request": "...", ... } ]
  }

Migration: a file whose top-level JSON has no "sessions" key is the old
schema-version-1 single-object format and is transparently upgraded.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from booking_agent import settings
from booking_agent.secure_file import ensure_private_dir, write_private_text

if TYPE_CHECKING:
    from booking_agent.authorization import BookingScopedCredential

SCHEMA_VERSION = 2           # per-Session object format version
STORE_SCHEMA_VERSION = 2     # SessionStore envelope version
SESSION_FILE_NAME = ".usp-session.json"
DEFAULT_MAX_SESSIONS = 30

VALID_PHASES = (
    "INIT",
    "PREREQ_OK",
    "BUSINESS_SELECTED",
    "CALENDAR_GATE",
    "AVAILABILITY_QUERIED",
    "SLOTS_PRESENTED",
    "SLOT_SELECTED",
    "BUYER_RESOLVED",
    "BOOKED",
    "PAYMENT_PENDING",
    "CONFIRMED",
    "FAILED",
)

TERMINAL_PHASES = frozenset({"CONFIRMED", "FAILED"})

# Phases where a selected future slot may still need cancel authority or resume.
_ACTIONABLE_IN_PROGRESS_PHASES = frozenset({
    "SLOT_SELECTED",
    "BUYER_RESOLVED",
    "BOOKED",
    "PAYMENT_PENDING",
    "CONFIRMED",
})

# Booking statuses where the appointment can no longer be canceled or moved.
# ``confirmed`` is absent on purpose: polling is done, but the buyer can still
# cancel or reschedule a confirmed booking.
INACTIVE_BOOKING_STATUSES = frozenset({"completed", "canceled", "no_show"})

BUYER_SOURCE_LINKCLI = "link-cli user-info retrieve"


@dataclass
class Business:
    profile_url: Optional[str] = None
    rest_endpoint: Optional[str] = None
    name: Optional[str] = None
    timezone: Optional[str] = None  # IANA zone of the selected business; None until a source names one


@dataclass
class AvailabilityWindow:
    from_: Optional[str] = None  # ISO 8601
    to: Optional[str] = None     # ISO 8601


@dataclass
class SlotRef:
    slot_id: str
    start: str
    end: str
    duration: Optional[str] = None
    price_amount: Optional[int] = None
    # Other resources' slot ids for the same start and end, tried in order if this one is taken.
    alternate_slot_ids: List[str] = field(default_factory=list)


@dataclass
class Buyer:
    name: str = ""
    email: str = ""
    phone: Optional[str] = None


@dataclass
class BookingRef:
    id: Optional[str] = None
    checkout_path: Optional[str] = None  # ucp | none
    payment_context: Optional[Dict[str, Any]] = None
    action_expires_at: Optional[str] = None  # ISO 8601; when the checkout expires

    # -- USP Section 10.1.6 per-resource authorization ------------------------
    # The booking-scoped credential issued at creation, keyed implicitly by
    # (endpoint, id): both are recorded so the credential is never presented to a
    # business other than the one that issued it.
    endpoint: Optional[str] = None       # issuing business REST endpoint
    credential: Optional[str] = None     # bsc_… value
    credential_expires_at: Optional[str] = None  # ISO 8601
    credential_jkt: Optional[str] = None  # cnf.jkt the credential is bound to
    credential_scope: Optional[List[str]] = None  # USP methods it authorizes, if narrowed
    revision: Optional[str] = None       # for If-Match conditional writes (Section 5.6)
    booking_url: Optional[str] = None    # human handoff target on 401/expiry
    status: Optional[str] = None         # last booking status read from or set by the business


@dataclass
class PaymentRef:
    status: Optional[str] = None  # pending | paid | failed
    spend_request_id: Optional[str] = None
    order_id: Optional[str] = None  # UCP complete_checkout order correlation (step 20)
    spend_approval_surfaced: bool = False  # True after pay request emitted the approval URL once
    spend_approval_user_surfaced: bool = False  # True after agent acked posting URL to the user
    approved_at: Optional[str] = None  # ISO 8601 when Link approval was observed (convenience copy)
    spend_action_surfaced: Optional[str] = None  # SpendNextAction.key already shown to the user
    payment_method_id: Optional[str] = None  # funding source of the last spend request, for a replacement
    # Last spend request a complete_checkout declined; spent, never offered again.
    declined_spend_request_id: Optional[str] = None
    declined_code: Optional[str] = None  # UCP message code of that decline
    declined_checkout_id: Optional[str] = None
    # complete_checkout captured the money but the booking is not confirmed; no
    # further charge may run until the booking is resolved.
    captured_unconfirmed: bool = False


@dataclass
class LastError:
    code: str
    message: str


@dataclass
class ActivityEntry:
    at: str
    kind: str  # command | error
    text: str
    code: Optional[str] = None


@dataclass
class Session:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    user_request: str = ""
    schema_version: int = SCHEMA_VERSION
    flow_phase: str = "INIT"
    env_file: Optional[str] = None  # absolute path from init --env-file or BOOKING_AGENT_ENV_FILE
    business: Business = field(default_factory=Business)
    service_timezone: Optional[str] = None  # selected registry result's zone; business.timezone wins
    buyer_timezone: Optional[str] = None  # IANA zone of the buyer's default Link shipping address
    buyer_timezone_source: Optional[str] = None
    buyer_timezone_error: Optional[str] = None  # sanitized code from the last failed lookup
    timezone_difference_shown: bool = False  # user picked a slot from a dual-zone list
    slots_presented_dual_zone: bool = False  # last slots list carried buyer-zone displays
    calendar_gate_completed: bool = False
    calendar_state: str = "unset"  # connected | skipped | unset
    calendar_mode: str = "unset"  # none | freebusy | events | unset
    link_auth_gate_completed: bool = False
    availability_window: AvailabilityWindow = field(default_factory=AvailabilityWindow)
    slots_presented: List[SlotRef] = field(default_factory=list)
    slot_id: Optional[str] = None
    slot_user_confirmed: bool = False
    service_id: Optional[str] = None
    selection_mismatch: Optional[str] = None  # less-direct label the user confirmed at business use
    timezone_conflict_confirmed: bool = False  # user accepted a service-location/timezone conflict
    live_service: Optional[Dict[str, Any]] = None  # GET /services/{id} snapshot before availability
    payment_timing: Optional[str] = None  # at_booking | at_service | deposit_required | None
    payment_route: Optional[str] = None  # free | at_service | at_booking | deposit_required
    selected_term_id: Optional[str] = None
    payment_schedules: Optional[List[Dict[str, Any]]] = None
    due_now_amount_minor: Optional[int] = None
    payment_disclosures: Optional[List[Dict[str, Any]]] = None
    disclosures_user_confirmed: bool = False
    total_confirmed_amount_minor: Optional[int] = None  # checkout total the user re-approved
    accepted_term: Optional[Dict[str, Any]] = None
    complete_checkout_idempotency_key: Optional[str] = None
    checkout_mode: Optional[str] = None  # ucp_native | direct; set by business use
    supported_checkout_modes: List[str] = field(default_factory=list)
    checkout_prereq_completed: bool = False
    ucp_checkout_rest_base: Optional[str] = None
    ucp_checkout_session_id: Optional[str] = None
    ucp_checkout_amount_minor: Optional[int] = None  # totals.total from create_checkout (minor units)
    ucp_merchant_quote: Optional[Dict[str, Any]] = None  # merchant checkout totals (Track A)
    ucp_checkout_links: Optional[List[Dict[str, Any]]] = None
    ucp_checkout_policies: Optional[List[Dict[str, Any]]] = None
    ucp_checkout_messages: Optional[List[Dict[str, Any]]] = None
    ucp_spt_network_id: Optional[str] = None  # from checkout payment_handlers config at book
    # Negotiated UCP capability set. Authoritative source is the checkout response
    # echo (checkout.ucp.capabilities/.version), NOT the platform-side profile
    # intersection - see ucp_capabilities.py. Persisted so complete_checkout can
    # reuse it without re-fetching the profile.
    ucp_protocol_version: Optional[str] = None  # authoritative negotiated UCP version (response echo)
    ucp_negotiated_capabilities: Dict[str, List[str]] = field(default_factory=dict)  # {namespace: [versions]}
    ucp_capabilities_authoritative: bool = False  # True once confirmed by the business response echo
    ucp_checkout_cancel_attempted: bool = False
    ucp_checkout_cancel_succeeded: Optional[bool] = None
    ucp_checkout_cancel_error: Optional[str] = None  # short, non-secret cancel failure summary
    buyer_source: str = "none"  # link-cli user-info retrieve | none
    buyer: Buyer = field(default_factory=Buyer)
    booking: BookingRef = field(default_factory=BookingRef)
    payment: PaymentRef = field(default_factory=PaymentRef)
    last_error: Optional[LastError] = None
    activity_log: List[ActivityEntry] = field(default_factory=list)
    discover_query: Optional[str] = None  # last registry search query (support reports read it)
    discover_location_lat: Optional[float] = None
    discover_location_lng: Optional[float] = None
    discover_location_radius_km: Optional[float] = None
    discover_registry_url: Optional[str] = None  # resolved POST URL for registry search
    discover_registry_request_body: Optional[Dict[str, Any]] = None  # raw search_services JSON
    discover_services: List[Dict[str, Any]] = field(default_factory=list)
    flow_timings: Dict[str, Dict[str, float]] = field(default_factory=dict)
    human_gate_timings: Dict[str, float] = field(default_factory=dict)
    skill_compliance_validated: bool = False
    # Reschedule working state for a CONFIRMED booking. slot_id and slots_presented
    # keep describing the booked slot until the business accepts the move.
    reschedule_original_slot_id: Optional[str] = None  # set by reschedule start
    reschedule_slots: List[SlotRef] = field(default_factory=list)
    reschedule_slot_id: Optional[str] = None
    reschedule_user_confirmed: bool = False
    # Consented support report (spec #307). The bytes are frozen at offer time and
    # are the only thing `flow support-report send` may deliver.
    support_report_declined: bool = False
    support_report_asked: bool = False
    support_report_bytes: Optional[str] = None
    support_report_recovery_command: Optional[str] = None
    support_report_send_failures: int = 0
    # Area the user confirmed for a place-based search:
    # {label, lat, lng, source, link_address_id, street_level, confirmed}.
    search_area: Optional[Dict[str, Any]] = None
    # Geocoded proposal shown to the user and not yet confirmed; ``purpose`` is
    # ``search`` or ``service_address``.
    pending_area: Optional[Dict[str, Any]] = None
    # Confirmed street address (USP DeliveryAddress fields) for home visits.
    service_address: Optional[Dict[str, Any]] = None
    service_address_label: Optional[str] = None
    area_resume_command: Optional[str] = None  # command to rerun after an address confirmation
    booking_notes: Optional[str] = None  # note for the business, set at buyer resolve
    calendar_timezone: Optional[str] = None  # primary Google Calendar zone (events mode only)


@dataclass
class SessionStore:
    schema_version: int = STORE_SCHEMA_VERSION
    max_sessions: int = DEFAULT_MAX_SESSIONS
    sessions_created: int = 0  # lifetime counter; never decrements on reset
    active_session_id: Optional[str] = None
    last_calendar_state: Optional[str] = None  # connected | skipped
    last_calendar_mode: Optional[str] = None  # freebusy | events | none
    sessions: List[Session] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def session_path() -> Path:
    """Return the path to the session store file.

    Override with ``BOOKING_AGENT_SESSION_DIR`` env var (used in tests to isolate
    each test's store from the real ``~/.booking-agent`` directory).
    """
    session_dir = os.environ.get("BOOKING_AGENT_SESSION_DIR")
    base = Path(session_dir) if session_dir else Path.home() / ".booking-agent"
    ensure_private_dir(base)
    return base / SESSION_FILE_NAME


# ---------------------------------------------------------------------------
# Store-level I/O
# ---------------------------------------------------------------------------

def load_store() -> SessionStore:
    p = session_path()
    if not p.exists():
        max_s = settings.get_int("BOOKING_AGENT_MAX_SESSIONS")
        return SessionStore(max_sessions=max_s)
    raw = json.loads(p.read_text())
    if "sessions" not in raw:
        store = _migrate_v1(raw)
    else:
        store = _store_from_dict(raw)
    if maintain_store(store):
        save_store(store)
    return store


def save_store(store: SessionStore) -> Path:
    # The store now carries booking-scoped credentials, so it is written 0600.
    p = session_path()
    write_private_text(p, json.dumps(_store_to_dict(store), indent=2, sort_keys=False))
    return p


# ---------------------------------------------------------------------------
# Session-level helpers (thin wrappers that the flow commands use)
# ---------------------------------------------------------------------------

def bind_session_observability(session: Optional[Session]) -> None:
    """Bind the flow correlation id for outbound HTTP and stderr events."""
    from booking_agent.observability import bind, reset

    if session and session.correlation_id:
        bind(session.correlation_id)
    else:
        reset()


def load_session() -> Optional[Session]:
    session = get_active_session(load_store())
    bind_session_observability(session)
    return session


SUPPORT_REPORT_FIELDS = (
    "support_report_declined",
    "support_report_asked",
    "support_report_bytes",
    "support_report_recovery_command",
    "support_report_send_failures",
)


def save_session(session: Session, *, write_support_report: bool = False) -> Path:
    """Persist an updated session back into the store.

    Commands often save an in-memory session loaded before ``emit_error`` froze a
    support report, so only callers that own the report may change those fields.
    """
    store = load_store()
    for i, s in enumerate(store.sessions):
        if s.id == session.id:
            if not write_support_report:
                for name in SUPPORT_REPORT_FIELDS:
                    setattr(session, name, getattr(s, name))
            store.sessions[i] = session
            return save_store(store)
    # Defensive: session not found in store — append it
    store.sessions.append(session)
    store.active_session_id = session.id
    return save_store(store)


def delete_session() -> bool:
    """Remove the active session entry from the store. Returns True if removed."""
    store = load_store()
    active = get_active_session(store)
    if active is None:
        return False
    store.sessions = [s for s in store.sessions if s.id != active.id]
    store.active_session_id = store.sessions[-1].id if store.sessions else None
    save_store(store)
    return True


def must_load() -> Session:
    s = load_session()
    if s is None:
        raise FileNotFoundError(
            "No active session. Run `booking-agent flow init` first."
        )
    return s


# ---------------------------------------------------------------------------
# Store mutation helpers
# ---------------------------------------------------------------------------

def get_active_session(store: SessionStore) -> Optional[Session]:
    if not store.sessions:
        return None
    if store.active_session_id:
        for s in store.sessions:
            if s.id == store.active_session_id:
                return s
    return store.sessions[-1]


def set_active_session(store: SessionStore, session_id: str) -> bool:
    """Point the store's active pointer at ``session_id``. Returns False if not found."""
    for s in store.sessions:
        if s.id == session_id:
            store.active_session_id = session_id
            return True
    return False


def add_session(store: SessionStore, session: Session) -> None:
    """Append a new session, set it as active, drop oldest non-actionable if over limit."""
    store.sessions.append(session)
    store.sessions_created += 1
    store.active_session_id = session.id
    maintain_store(store)


def session_slot_end(session: Session, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Return the UTC end time of the selected slot, if known."""
    _ = now
    if not session.slot_id:
        return None
    for slot in session.slots_presented:
        if slot.slot_id != session.slot_id:
            continue
        try:
            end = datetime.fromisoformat(slot.end.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return end
    return None


def is_actionable(session: Session, *, now: Optional[datetime] = None) -> bool:
    """True while the consumer may still need cancel/resume for this booking."""
    if session.flow_phase == "FAILED":
        return False
    if session.booking.status in INACTIVE_BOOKING_STATUSES:
        return False
    now = now or datetime.now(timezone.utc)
    slot_end = session_slot_end(session)

    if session.booking.id:
        if slot_end is None:
            return True
        return slot_end >= now

    if session.flow_phase in _ACTIONABLE_IN_PROGRESS_PHASES and session.slot_id:
        if slot_end is None:
            return True
        return slot_end >= now

    return False


def archive_expired_session(session: Session, *, now: Optional[datetime] = None) -> bool:
    """Strip secrets from a session whose appointment has passed. Returns True if modified."""
    now = now or datetime.now(timezone.utc)
    slot_end = session_slot_end(session)
    if slot_end is None or slot_end >= now:
        return False
    clear_credential(session)
    session.buyer = Buyer()
    session.buyer_source = "none"
    session.search_area = None
    session.pending_area = None
    session.service_address = None
    session.service_address_label = None
    session.booking_notes = None
    # Keep dispute join keys (#189): spend request id, order id, payment status, approval time.
    # Wipe only ephemeral approval-gate flags and anything secret-bearing.
    session.payment.spend_approval_surfaced = False
    session.payment.spend_approval_user_surfaced = False
    session.payment.spend_action_surfaced = None
    return True


def maintain_store(store: SessionStore, *, now: Optional[datetime] = None) -> bool:
    """Archive past appointments and trim only non-actionable sessions."""
    now = now or datetime.now(timezone.utc)
    modified = False
    for session in store.sessions:
        if archive_expired_session(session, now=now):
            modified = True
    before_len = len(store.sessions)
    before_active = store.active_session_id
    _trim_non_actionable_sessions(store, now=now)
    if len(store.sessions) != before_len or store.active_session_id != before_active:
        modified = True
    return modified


def _trim_non_actionable_sessions(store: SessionStore, *, now: Optional[datetime] = None) -> None:
    now = now or datetime.now(timezone.utc)
    while len(store.sessions) > store.max_sessions:
        droppable = [
            (idx, sess)
            for idx, sess in enumerate(store.sessions)
            if not is_actionable(sess, now=now)
        ]
        if not droppable:
            break
        drop_idx = min(droppable, key=lambda item: item[1].created_at)[0]
        dropped = store.sessions.pop(drop_idx)
        if store.active_session_id == dropped.id:
            store.active_session_id = (
                store.sessions[-1].id if store.sessions else None
            )


def forget_session(store: SessionStore, session_id: str) -> bool:
    """Remove one session by id. Does not touch Link/Google identity stores."""
    for idx, session in enumerate(store.sessions):
        if session.id != session_id:
            continue
        store.sessions.pop(idx)
        if store.active_session_id == session_id:
            store.active_session_id = (
                store.sessions[-1].id if store.sessions else None
            )
        return True
    return False


# ---------------------------------------------------------------------------
# Booking-scoped credentials (USP Section 10.1.6)
# ---------------------------------------------------------------------------

def record_issued_credential(
    session: Session,
    credential: Optional["BookingScopedCredential"],
    *,
    endpoint: Optional[str],
    booking_url: Optional[str] = None,
    revision: Optional[str] = None,
) -> None:
    """Persist the credential a business issued at resource creation.

    The endpoint is stored alongside it so ``credential_for`` can refuse to hand
    it to a different business.
    """
    session.booking.endpoint = endpoint
    if booking_url:
        session.booking.booking_url = booking_url
    if revision:
        session.booking.revision = revision
    if credential is None:
        return
    session.booking.credential = credential.value
    session.booking.credential_expires_at = credential.expires_at.isoformat()
    session.booking.credential_jkt = credential.cnf.jkt if credential.cnf else None
    session.booking.credential_scope = list(credential.scope) if credential.scope else None


def credential_for(
    session: Session, *, endpoint: Optional[str],
) -> Optional["BookingScopedCredential"]:
    """Return the stored credential, but only for the business that issued it.

    A booking-scoped credential is a secret scoped to one resource at one
    business.  Presenting it elsewhere would leak it to a party with no claim to
    it, so an endpoint mismatch returns None rather than the credential.
    """
    from booking_agent.authorization import BookingScopedCredential

    ref = session.booking
    if not ref.credential or not ref.credential_expires_at:
        return None
    if endpoint and ref.endpoint and endpoint != ref.endpoint:
        return None
    payload: Dict[str, Any] = {
        "value": ref.credential,
        "expires_at": ref.credential_expires_at,
    }
    if ref.credential_jkt:
        payload["cnf"] = {"jkt": ref.credential_jkt}
    if ref.credential_scope:
        payload["scope"] = ref.credential_scope
    try:
        return BookingScopedCredential.model_validate(payload)
    except Exception:
        return None


def credential_for_session_resource(
    session: Session,
    *,
    endpoint: Optional[str],
    resolved_business_endpoint: Optional[str] = None,
) -> Optional["BookingScopedCredential"]:
    """Return a credential for a related binding of this session's business.

    Exact issuer matches remain the default. UCP-native checkout is the sole
    cross-binding exception: checkout issues the booking credential, while the
    resulting booking resource is read from the USP REST binding in the same
    business profile.
    """
    exact = credential_for(session, endpoint=endpoint)
    if exact is not None:
        return exact

    issuer = session.booking.endpoint
    checkout_endpoint = session.ucp_checkout_rest_base
    usp_endpoint = session.business.rest_endpoint or resolved_business_endpoint
    if (
        issuer
        and endpoint
        and checkout_endpoint
        and usp_endpoint
        and issuer == checkout_endpoint
        and endpoint == usp_endpoint
    ):
        return credential_for(session, endpoint=issuer)
    return None


def clear_credential(session: Session) -> None:
    """Drop the credential at a terminal state; it is invalid from that point."""
    ref = session.booking
    ref.credential = None
    ref.credential_expires_at = None
    ref.credential_jkt = None
    ref.credential_scope = None


def endpoint_still_in_use(
    store: SessionStore, endpoint: Optional[str], *, excluding_session_id: Optional[str] = None,
) -> bool:
    """True when any other session still holds a live credential at *endpoint*.

    This is what makes PoP key destruction safe.  The key is per-endpoint while
    credentials are per-resource, so dropping the key while another booking's
    credential is still bound to it would leave that booking permanently
    uncancellable -- USP defines no re-issuance path for a key you no longer
    hold.
    """
    if not endpoint:
        return False
    now = datetime.now(timezone.utc)
    for sess in store.sessions:
        if excluding_session_id and sess.id == excluding_session_id:
            continue
        ref = sess.booking
        if ref.endpoint != endpoint or not ref.credential:
            continue
        if not ref.credential_expires_at:
            return True
        try:
            expires = datetime.fromisoformat(ref.credential_expires_at)
        except ValueError:
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > now:
            return True
    return False


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _store_to_dict(store: SessionStore) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "schema_version": store.schema_version,
        "max_sessions": store.max_sessions,
        "sessions_created": store.sessions_created,
        "active_session_id": store.active_session_id,
        "sessions": [_session_to_dict(s) for s in store.sessions],
    }
    if store.last_calendar_state is not None:
        d["last_calendar_state"] = store.last_calendar_state
    if store.last_calendar_mode is not None:
        d["last_calendar_mode"] = store.last_calendar_mode
    return d


def _store_from_dict(raw: Dict[str, Any]) -> SessionStore:
    max_s = int(os.environ.get(
        "BOOKING_AGENT_MAX_SESSIONS",
        raw.get("max_sessions") or settings.get_int("BOOKING_AGENT_MAX_SESSIONS"),
    ))
    return SessionStore(
        schema_version=raw.get("schema_version", STORE_SCHEMA_VERSION),
        max_sessions=max_s,
        sessions_created=raw.get("sessions_created", 0),
        active_session_id=raw.get("active_session_id"),
        last_calendar_state=raw.get("last_calendar_state"),
        last_calendar_mode=raw.get("last_calendar_mode"),
        sessions=[_session_from_dict(s) for s in (raw.get("sessions") or [])],
    )


def _session_to_dict(s: Session) -> Dict[str, Any]:
    d = asdict(s)
    aw = d.pop("availability_window")
    d["availability_window"] = {"from": aw.get("from_"), "to": aw.get("to")}
    buyer = d.get("buyer")
    if isinstance(buyer, dict):
        # Agent half of spec #159: do not persist buyer contact on disk.
        # Name and buyer_source stay; email/phone are rehydrated from link-cli.
        buyer["email"] = ""
        buyer["phone"] = None
    return d


def _known_fields(cls, raw: Dict[str, Any]):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in names})


def _session_from_dict(raw: Dict[str, Any]) -> Session:
    raw = dict(raw)  # avoid mutating caller's dict
    aw_in = raw.get("availability_window") or {}
    raw["availability_window"] = {
        "from_": aw_in.get("from") or aw_in.get("from_"),
        "to": aw_in.get("to"),
    }
    return Session(
        id=raw.get("id") or str(uuid.uuid4()),
        correlation_id=raw.get("correlation_id") or str(uuid.uuid4()),
        created_at=raw.get("created_at") or datetime.now(timezone.utc).isoformat(),
        user_request=raw.get("user_request", ""),
        schema_version=raw.get("schema_version", SCHEMA_VERSION),
        flow_phase=raw.get("flow_phase", "INIT"),
        env_file=raw.get("env_file"),
        business=Business(**(raw.get("business") or {})),
        service_timezone=raw.get("service_timezone"),
        buyer_timezone=raw.get("buyer_timezone"),
        buyer_timezone_source=raw.get("buyer_timezone_source"),
        buyer_timezone_error=raw.get("buyer_timezone_error"),
        timezone_difference_shown=bool(raw.get("timezone_difference_shown", False)),
        slots_presented_dual_zone=bool(raw.get("slots_presented_dual_zone", False)),
        calendar_gate_completed=raw.get("calendar_gate_completed", False),
        calendar_state=raw.get("calendar_state", "unset"),
        calendar_mode=raw.get("calendar_mode", "unset"),
        link_auth_gate_completed=raw.get("link_auth_gate_completed", False),
        availability_window=AvailabilityWindow(**raw["availability_window"]),
        slots_presented=[SlotRef(**s) for s in (raw.get("slots_presented") or [])],
        slot_id=raw.get("slot_id"),
        slot_user_confirmed=raw.get("slot_user_confirmed", False),
        service_id=raw.get("service_id"),
        selection_mismatch=raw.get("selection_mismatch"),
        timezone_conflict_confirmed=bool(raw.get("timezone_conflict_confirmed", False)),
        live_service=raw.get("live_service"),
        payment_timing=raw.get("payment_timing"),
        payment_route=raw.get("payment_route"),
        selected_term_id=raw.get("selected_term_id"),
        payment_schedules=list(raw.get("payment_schedules") or []) or None,
        due_now_amount_minor=raw.get("due_now_amount_minor"),
        payment_disclosures=list(raw.get("payment_disclosures") or []) or None,
        disclosures_user_confirmed=bool(raw.get("disclosures_user_confirmed", False)),
        total_confirmed_amount_minor=raw.get("total_confirmed_amount_minor"),
        accepted_term=raw.get("accepted_term"),
        complete_checkout_idempotency_key=raw.get("complete_checkout_idempotency_key"),
        checkout_mode=raw.get("checkout_mode"),
        supported_checkout_modes=list(raw.get("supported_checkout_modes") or []),
        checkout_prereq_completed=raw.get("checkout_prereq_completed", False),
        ucp_checkout_rest_base=raw.get("ucp_checkout_rest_base"),
        ucp_checkout_session_id=raw.get("ucp_checkout_session_id"),
        ucp_checkout_amount_minor=raw.get("ucp_checkout_amount_minor"),
        ucp_merchant_quote=raw.get("ucp_merchant_quote"),
        ucp_checkout_links=list(raw.get("ucp_checkout_links") or []) or None,
        ucp_checkout_policies=list(raw.get("ucp_checkout_policies") or []) or None,
        ucp_checkout_messages=list(raw.get("ucp_checkout_messages") or []) or None,
        ucp_spt_network_id=raw.get("ucp_spt_network_id"),
        ucp_protocol_version=raw.get("ucp_protocol_version"),
        ucp_negotiated_capabilities=dict(raw.get("ucp_negotiated_capabilities") or {}),
        ucp_capabilities_authoritative=raw.get("ucp_capabilities_authoritative", False),
        ucp_checkout_cancel_attempted=bool(raw.get("ucp_checkout_cancel_attempted", False)),
        ucp_checkout_cancel_succeeded=raw.get("ucp_checkout_cancel_succeeded"),
        ucp_checkout_cancel_error=raw.get("ucp_checkout_cancel_error"),
        buyer_source=raw.get("buyer_source", "none"),
        buyer=Buyer(**(raw.get("buyer") or {})),
        booking=_known_fields(BookingRef, raw.get("booking") or {}),
        payment=PaymentRef(**(raw.get("payment") or {})),
        last_error=(LastError(**raw["last_error"]) if raw.get("last_error") else None),
        activity_log=[ActivityEntry(**e) for e in (raw.get("activity_log") or [])],
        discover_query=raw.get("discover_query"),
        discover_location_lat=raw.get("discover_location_lat"),
        discover_location_lng=raw.get("discover_location_lng"),
        discover_location_radius_km=raw.get("discover_location_radius_km"),
        discover_registry_url=raw.get("discover_registry_url"),
        discover_registry_request_body=raw.get("discover_registry_request_body"),
        discover_services=list(raw.get("discover_services") or []),
        flow_timings=dict(raw.get("flow_timings") or {}),
        human_gate_timings=dict(raw.get("human_gate_timings") or {}),
        skill_compliance_validated=raw.get("skill_compliance_validated", False),
        reschedule_original_slot_id=raw.get("reschedule_original_slot_id"),
        reschedule_slots=[SlotRef(**s) for s in (raw.get("reschedule_slots") or [])],
        reschedule_slot_id=raw.get("reschedule_slot_id"),
        reschedule_user_confirmed=bool(raw.get("reschedule_user_confirmed", False)),
        support_report_declined=bool(raw.get("support_report_declined", False)),
        support_report_asked=bool(raw.get("support_report_asked", False)),
        support_report_bytes=raw.get("support_report_bytes"),
        support_report_recovery_command=raw.get("support_report_recovery_command"),
        support_report_send_failures=int(raw.get("support_report_send_failures") or 0),
        search_area=raw.get("search_area"),
        pending_area=raw.get("pending_area"),
        service_address=raw.get("service_address"),
        service_address_label=raw.get("service_address_label"),
        area_resume_command=raw.get("area_resume_command"),
        booking_notes=raw.get("booking_notes"),
        calendar_timezone=raw.get("calendar_timezone"),
    )


def _migrate_v1(raw: Dict[str, Any]) -> SessionStore:
    """Wrap a schema-version-1 single-object file in a v2 store envelope."""
    session = _session_from_dict(raw)
    max_s = settings.get_int("BOOKING_AGENT_MAX_SESSIONS")
    return SessionStore(
        schema_version=STORE_SCHEMA_VERSION,
        max_sessions=max_s,
        sessions_created=1,  # the migrated session counts toward the lifetime total
        active_session_id=session.id,
        sessions=[session],
    )
