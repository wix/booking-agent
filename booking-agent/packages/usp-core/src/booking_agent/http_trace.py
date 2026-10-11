"""Shared httpx tracing for every external HTTP call the CLI makes.

Tracing is **off by default** in production. Set ``BOOKING_AGENT_HTTP_TRACE=1`` to
append redacted request/response records to ``~/.booking-agent/http_trace.log``.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from .circuit_breaker import HostCircuitBreaker, default_breaker
from .http_deadlines import coerce_timeout
from .observability import HEADER, current, emit_event
from .secure_file import FILE_MODE, ensure_private_dir

_REDACT_HEADERS = {"authorization", "dpop", "dpop-nonce", "set-cookie", "cookie"}
_REDACT_BODY_KEYS = frozenset({
    "token",
    "credential",
    "shared_payment_token",
    "client_secret",
    "access_token",
    "refresh_token",
    "email",
    "phone",
    "phone_number",
    "first_name",
    "last_name",
    "name",
})
# Keys are compared lowercased with "_" and "-" removed, so camelCase aliases match too.
_ADDRESS_LEAF_KEYS = frozenset({
    "line1",
    "line2",
    "addressline",
    "addressline1",
    "addressline2",
    "street",
    "streetaddress",
    "postalcode",
    "postcode",
    "zip",
    "zipcode",
})
# Too generic to redact everywhere (business location, booking state), so only under a buyer.
_BUYER_ADDRESS_KEYS = frozenset({
    "city",
    "locality",
    "region",
    "subdivision",
    "state",
    "administrativearea",
    "country",
    "countrycode",
})
_RAW_ADDRESS_LEAF = re.compile(
    r'"((?:address_?)?line_?[12]?|street\w*|postal_?code|post_?code|zip(?:_?code)?'
    r'|city|locality|region|subdivision|state|country(?:_?code)?)"\s*:\s*"[^"]*"',
    re.IGNORECASE,
)
_RAW_ADDRESS_OBJECT = re.compile(r'"(\w*address)"\s*:\s*\{[^{}]*\}', re.IGNORECASE)
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def tracing_enabled() -> bool:
    return os.environ.get("BOOKING_AGENT_HTTP_TRACE", "").strip() == "1"


def trace_path() -> Path:
    """Location of the trace log; follows ``BOOKING_AGENT_SESSION_DIR`` like the session store."""
    session_dir = os.environ.get("BOOKING_AGENT_SESSION_DIR")
    base = Path(session_dir) if session_dir else Path.home() / ".booking-agent"
    ensure_private_dir(base)
    return base / "http_trace.log"


def _max_trace_bytes() -> int:
    raw = os.environ.get("BOOKING_AGENT_HTTP_TRACE_MAX_BYTES", "").strip()
    if not raw:
        return _DEFAULT_MAX_BYTES
    try:
        return max(int(raw), 1024)
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _redact(headers: httpx.Headers) -> Dict[str, str]:
    return {k: ("<redacted>" if k.lower() in _REDACT_HEADERS else v) for k, v in headers.items()}


def _redact_all_leaves(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_all_leaves(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_all_leaves(item) for item in value]
    return None if value is None else "<redacted>"


def _redact_json_value(value: Any, *, in_buyer: bool = False) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            key_l = str(key).lower()
            key_n = key_l.replace("_", "").replace("-", "")
            if key_l in _REDACT_BODY_KEYS or "credential" in key_l or key_l.endswith("_token"):
                out[key] = "<redacted>"
            elif key_n.endswith("address"):
                out[key] = _redact_all_leaves(item)
            elif key_n in _ADDRESS_LEAF_KEYS or (in_buyer and key_n in _BUYER_ADDRESS_KEYS):
                out[key] = _redact_all_leaves(item)
            elif key_l == "payment" and isinstance(item, dict):
                out[key] = _redact_json_value(item)
            elif key_l == "instruments" and isinstance(item, list):
                out[key] = [_redact_json_value(i) for i in item]
            elif key_l == "buyer" and isinstance(item, dict):
                out[key] = _redact_json_value(item, in_buyer=True)
            else:
                out[key] = _redact_json_value(item, in_buyer=in_buyer)
        return out
    if isinstance(value, list):
        return [_redact_json_value(item, in_buyer=in_buyer) for item in value]
    return value


def _redact_body_text(body: str | None) -> str | None:
    if not body:
        return body
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        redacted = body
        for pattern in (
            r'"credential"\s*:\s*"[^"]*"',
            r'"token"\s*:\s*"[^"]*"',
            r'"shared_payment_token"\s*:\s*"[^"]*"',
            r'"email"\s*:\s*"[^"]*"',
            r'"phone(?:_number)?"\s*:\s*"[^"]*"',
        ):
            redacted = re.sub(pattern, '"<redacted>"', redacted)
        redacted = _RAW_ADDRESS_OBJECT.sub(r'"\1": "<redacted>"', redacted)
        redacted = _RAW_ADDRESS_LEAF.sub(r'"\1": "<redacted>"', redacted)
        return redacted
    return json.dumps(_redact_json_value(parsed), default=str)


def _rotate_trace_if_needed(path: Path) -> None:
    max_bytes = _max_trace_bytes()
    try:
        if path.is_file() and path.stat().st_size > max_bytes:
            backup = path.with_suffix(".log.1")
            if backup.is_file():
                backup.unlink()
            path.replace(backup)
    except OSError:
        pass


def _append(record: Dict[str, Any]) -> None:
    if not tracing_enabled():
        return
    path = trace_path()
    _rotate_trace_if_needed(path)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, FILE_MODE)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _request_body_for_trace(request: httpx.Request) -> str | None:
    try:
        content = request.content
    except httpx.RequestNotRead:
        return None
    if not content:
        return None
    return _redact_body_text(content.decode("utf-8", errors="replace"))


async def _stamp_trace_t0(request: httpx.Request) -> None:
    request.extensions["trace_t0"] = time.monotonic()


async def _record_step_http_elapsed(response: httpx.Response) -> None:
    t0 = response.request.extensions.get("trace_t0")
    if t0 is None:
        return
    from .step_timing import record_http_elapsed_ms

    record_http_elapsed_ms(round((time.monotonic() - t0) * 1000, 1))


# Last failing (>= 400) exchange, for the consented support report (spec #307).
# Module-level rather than a ContextVar: hooks run inside ``asyncio.run``, whose
# context copy never flows back to the synchronous caller.
_last_http_failure: Optional[Dict[str, Any]] = None
_FAILURE_BODY_MAX_BYTES = 64 * 1024
_TOKEN_RE = re.compile(r"^[a-z0-9_.]{1,64}$")
_PROBLEM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_MEDIA_TYPE_RE = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_ID_SEGMENT_RE = re.compile(
    r"^(?:[A-Za-z0-9]+[_-])*[A-Za-z0-9]*\d[A-Za-z0-9_-]*$|^[0-9a-fA-F-]{16,}$"
)
_SAFE_SEGMENT_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,40}$")


def last_http_failure() -> Optional[Dict[str, Any]]:
    """The last failing exchange recorded in this process, or None."""
    return dict(_last_http_failure) if _last_http_failure else None


def clear_last_http_failure() -> None:
    global _last_http_failure
    _last_http_failure = None


def _template_path(path: str) -> str:
    out = []
    for segment in path.split("/"):
        if not segment or _SAFE_SEGMENT_RE.match(segment) or _ID_SEGMENT_RE.match(segment):
            out.append(segment)
        else:
            out.append("{x}")
    return "/".join(out)[:512]


def _safe_problem_type(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    if value.startswith("https://"):
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if parts.username or parts.password or parts.query or parts.fragment:
            return None
        if not _PROBLEM_TOKEN_RE.match(parts.path or "/") and parts.path:
            return None
        return value
    return value if _PROBLEM_TOKEN_RE.match(value) and "/" not in value else None


def _failure_body_json(response: httpx.Response) -> Any:
    media = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not (media == "application/json" or media.endswith("+json")):
        return None
    try:
        content = response.content
    except httpx.ResponseNotRead:
        return None
    if not content or len(content) > _FAILURE_BODY_MAX_BYTES:
        return None
    return json.loads(content.decode("utf-8"))


def _ucp_message_codes(body: Any) -> tuple[list, int]:
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        return [], 0
    codes, dropped = [], 0
    for item in messages:
        code = item.get("code") if isinstance(item, dict) else None
        if not isinstance(code, str) or not _TOKEN_RE.match(code):
            dropped += 1
            continue
        severity = item.get("severity")
        codes.append({
            "code": code,
            "severity": severity if isinstance(severity, str) and _TOKEN_RE.match(severity) else None,
        })
    return codes, dropped


async def _record_http_failure(response: httpx.Response) -> None:
    global _last_http_failure
    try:
        if response.status_code < 400:
            return
        request = response.request
        record: Dict[str, Any] = {
            "method": request.method,
            "host": request.url.host or "",
            "path": _template_path(request.url.path or "/"),
            "status": response.status_code,
            "content_type": None,
            "problem_type": None,
            "ucp_message_codes": [],
            "dropped_codes": 0,
            "elapsed_ms": None,
        }
        _last_http_failure = record
        media = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if media and len(media) <= 128 and _MEDIA_TYPE_RE.match(media):
            record["content_type"] = media
        t0 = request.extensions.get("trace_t0")
        if t0 is not None:
            record["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) <= _FAILURE_BODY_MAX_BYTES:
            await response.aread()
        body = _failure_body_json(response)
        if isinstance(body, dict):
            record["problem_type"] = _safe_problem_type(body.get("type"))
            nested = body.get("checkout") if isinstance(body.get("checkout"), dict) else body
            codes, dropped = _ucp_message_codes(nested)
            record["ucp_message_codes"] = codes[:20]
            record["dropped_codes"] = dropped + max(len(codes) - 20, 0)
    except Exception:
        pass


async def _inject_correlation_header(request: httpx.Request) -> None:
    cid = current()
    if cid:
        request.headers[HEADER] = cid


async def _log_request(request: httpx.Request) -> None:
    if not tracing_enabled():
        return
    trace_id = str(uuid.uuid4())
    request.extensions["trace_id"] = trace_id
    request.extensions["trace_t0"] = time.monotonic()
    body = _request_body_for_trace(request)
    record: Dict[str, Any] = {
        "trace_id": trace_id,
        "direction": "request",
        "method": request.method,
        "url": str(request.url),
        "headers": _redact(request.headers),
        "body": body,
    }
    cid = current()
    if cid:
        record["correlation_id"] = cid
    _append(record)


async def _log_response(response: httpx.Response) -> None:
    if not tracing_enabled():
        return
    await response.aread()
    t0 = response.request.extensions.get("trace_t0")
    elapsed_ms = round((time.monotonic() - t0) * 1000, 1) if t0 else None
    record: Dict[str, Any] = {
        "trace_id": response.request.extensions.get("trace_id"),
        "direction": "response",
        "status_code": response.status_code,
        "url": str(response.request.url),
        "headers": _redact(response.headers),
        "body": _redact_body_text(response.text),
        "elapsed_ms": elapsed_ms,
    }
    cid = current()
    if cid:
        record["correlation_id"] = cid
    _append(record)


class _CircuitTransport(httpx.AsyncBaseTransport):
    """Records timeouts and 5xx/429 against the per-host circuit breaker."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, breaker: HostCircuitBreaker
    ) -> None:
        self._inner = inner
        self._breaker = breaker

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._breaker.guard(request.url)
        try:
            response = await self._inner.handle_async_request(request)
        except httpx.TimeoutException as exc:
            kind = "connect" if isinstance(exc, httpx.ConnectTimeout) else (
                "read" if isinstance(exc, httpx.ReadTimeout) else "other"
            )
            emit_event("http_timeout", kind=kind, host=request.url.host or "")
            self._breaker.record_failure(request.url)
            raise
        except httpx.NetworkError:
            self._breaker.record_failure(request.url)
            raise
        if response.status_code >= 500 or response.status_code == 429:
            self._breaker.record_failure(request.url)
        else:
            self._breaker.record_success(request.url)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def traced_async_client(
    **kwargs: Any,
) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` with optional redacted trace logging."""
    circuit: Optional[HostCircuitBreaker] = kwargs.pop("circuit", None)
    if circuit is None:
        circuit = default_breaker()
    kwargs["timeout"] = coerce_timeout(kwargs.get("timeout"))
    inner = kwargs.pop("transport", None)
    if inner is None:
        inner = httpx.AsyncHTTPTransport()
    kwargs["transport"] = _CircuitTransport(inner, circuit)

    hooks = dict(kwargs.pop("event_hooks", {}) or {})
    hooks["request"] = [
        *hooks.get("request", []),
        _stamp_trace_t0,
        _inject_correlation_header,
    ]
    hooks["response"] = [
        *hooks.get("response", []), _record_step_http_elapsed, _record_http_failure,
    ]
    if tracing_enabled():
        hooks["request"] = [*hooks["request"], _log_request]
        hooks["response"] = [*hooks["response"], _log_response]
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)


def purge_trace_log() -> bool:
    """Delete the trace log if present (used by sessions forget / hygiene)."""
    path = trace_path()
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        pass
    return False
