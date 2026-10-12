"""link-cli subprocess helpers for payment flow."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

def default_link_cli_bin() -> str:
    from booking_agent import settings

    return settings.get("LINK_CLI_BIN") or "link-cli"

_SPEND_TERMINAL = frozenset({"denied", "canceled", "cancelled", "expired", "failed"})
# Spend requests that may include a shared_payment_token in retrieve JSON (link-cli).
# ``submitted`` is where an issuance 3DS challenge lands once the bank step passes.
_SPEND_SPT_RETRIEVE_OK = frozenset({"approved", "submitted", "succeeded"})
SPEND_REQUIRES_ACTION = "requires_action"
NEXT_ACTION_AUTO_RESUME = "auto_resume"


class LinkCliUserInfoError(Exception):
    """link-cli user-info retrieve failed or returned unusable JSON."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def retrieve_user_info(*, link_cli_bin: Optional[str] = None) -> Dict[str, Any]:
    """Run ``link-cli user-info retrieve --format json`` and return the object."""
    bin_name = link_cli_bin or default_link_cli_bin()
    if shutil.which(bin_name) is None:
        raise LinkCliUserInfoError(
            "link_cli_not_found",
            f"`{bin_name}` not found on PATH. Install link-cli "
            "(https://github.com/stripe/link-cli) and authenticate first.",
        )
    try:
        completed = subprocess.run(
            [bin_name, "user-info", "retrieve", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise LinkCliUserInfoError(
            "link_cli_failed",
            f"link-cli user-info retrieve failed: {e.stderr.strip() or e}",
        ) from e
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise LinkCliUserInfoError("link_cli_bad_json", str(e)) from e
    if not isinstance(payload, dict):
        raise LinkCliUserInfoError(
            "link_cli_bad_json",
            "link-cli user-info retrieve returned non-object JSON",
        )
    return payload


class LinkCliShippingAddressError(Exception):
    """``link-cli shipping-address list`` failed. ``code`` is safe to emit; no address data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def retrieve_shipping_addresses(*, link_cli_bin: Optional[str] = None) -> list[Dict[str, Any]]:
    """Run ``link-cli shipping-address list --format json`` and return the entries."""
    bin_name = link_cli_bin or default_link_cli_bin()
    if shutil.which(bin_name) is None:
        raise LinkCliShippingAddressError("link_cli_not_found")
    try:
        completed = subprocess.run(
            [bin_name, "shipping-address", "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise LinkCliShippingAddressError("link_cli_failed") from None
    if completed.returncode != 0:
        raise LinkCliShippingAddressError("link_cli_failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise LinkCliShippingAddressError("link_cli_bad_json") from None
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("shipping_addresses") or [payload]
    if not isinstance(payload, list):
        raise LinkCliShippingAddressError("link_cli_bad_json")
    return [row for row in payload if isinstance(row, dict)]


def default_shipping_address(entries: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The ``is_default`` entry's address object, or the only entry when there is one."""
    chosen = next((row for row in entries if row.get("is_default") is True), None)
    if chosen is None and len(entries) == 1:
        chosen = entries[0]
    if chosen is None:
        return None
    address = chosen.get("address")
    return address if isinstance(address, dict) else None


def retrieve_default_shipping_address(*, link_cli_bin: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return default_shipping_address(retrieve_shipping_addresses(link_cli_bin=link_cli_bin))


def buyer_fields_from_user_info(payload: Dict[str, Any]) -> tuple[str, str, Optional[str]]:
    """Parse name, email, phone from a link-cli user-info object."""
    name = payload.get("name") or " ".join(
        v for v in [payload.get("first_name"), payload.get("last_name")] if v
    )
    email = payload.get("email", "") or ""
    phone = payload.get("phone") or payload.get("phone_number")
    return str(name), str(email), phone if phone is None else str(phone)


@dataclass
class SpendNextAction:
    """``status_details.requires_action.next_action`` from a spend request."""

    type: str
    resolution: str
    display_message: Optional[str] = None
    action_url: Optional[str] = None

    @property
    def auto_resume(self) -> bool:
        return self.resolution == NEXT_ACTION_AUTO_RESUME

    def key(self, spend_request_id: str) -> str:
        """Identity of one surfaced action, so it is shown to the user once."""
        return f"{spend_request_id}:{self.type}"


def parse_next_action(sr: Dict[str, Any]) -> Optional[SpendNextAction]:
    details = sr.get("status_details")
    if not isinstance(details, dict):
        return None
    requires_action = details.get("requires_action")
    if not isinstance(requires_action, dict):
        return None
    raw = requires_action.get("next_action")
    if not isinstance(raw, dict):
        return None
    action_type = raw.get("type")
    resolution = raw.get("resolution")
    if not isinstance(action_type, str) or not isinstance(resolution, str):
        return None
    message = raw.get("display_message")
    url = raw.get("action_url")
    return SpendNextAction(
        type=action_type,
        resolution=resolution,
        display_message=message if isinstance(message, str) and message.strip() else None,
        action_url=url if isinstance(url, str) and url.strip() else None,
    )


@dataclass
class SpendRequestStatus:
    id: str
    status: str
    approval_url: Optional[str] = None
    approved_at: Optional[str] = None
    next_action: Optional[SpendNextAction] = None


class SpendActionRequired(Exception):
    """Link needs the buyer to act before this spend request can grant a credential."""

    def __init__(self, spend_request_id: str, action: SpendNextAction) -> None:
        super().__init__(
            f"spend request {spend_request_id} requires action: {action.type}"
        )
        self.spend_request_id = spend_request_id
        self.action = action


def _link_cli_cmd(*args: str) -> list[str]:
    bin_name = shutil.which(default_link_cli_bin()) or default_link_cli_bin()
    return [bin_name, *args]


def _coerce_shared_payment_token(raw: Any) -> Optional[str]:
    """Normalize SPT from link-cli JSON (string or small object envelope)."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("token", "value", "secret", "shared_payment_token", "id"):
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _extract_shared_payment_token_from_spend_payload(sr: Dict[str, Any]) -> str:
    """Parse SPT from a spend-request retrieve JSON object (approved or settled)."""
    status = sr.get("status", "unknown")
    if status in _SPEND_TERMINAL:
        raise RuntimeError(f"spend request terminal status: {status}")
    if status not in _SPEND_SPT_RETRIEVE_OK:
        raise RuntimeError(
            f"spend request has no usable SPT in this state (status={status}); "
            f"expected one of: {sorted(_SPEND_SPT_RETRIEVE_OK)}"
        )
    for key in ("shared_payment_token", "credential"):
        token = _coerce_shared_payment_token(sr.get(key))
        if token:
            return token
    raise RuntimeError(
        "Spend retrieve response did not include a usable shared_payment_token. "
        "Create the spend request with credential_type shared_payment_token for UCP-native checkout."
    )


def _retrieve_spend_request_with_shared_payment_token(
    spend_request_id: str,
) -> Dict[str, Any]:
    """Retrieve one spend snapshot including its shared payment token."""
    completed = subprocess.run(
        _link_cli_cmd(
            "spend-request",
            "retrieve",
            spend_request_id,
            "--include",
            "shared_payment_token",
            "--format",
            "json",
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"link-cli SPT retrieve failed for {spend_request_id}"
        )
    payload = _parse_link_cli_json(completed.stdout)
    sr = payload[0] if isinstance(payload, list) else payload
    if not isinstance(sr, dict):
        raise RuntimeError("link-cli returned non-object JSON for spend-request retrieve")
    return sr


class SharedPaymentTokenUnavailable(RuntimeError):
    """Link approved the spend request but returned no shared payment token."""

    def __init__(self, spend_request_id: str, status: str, waited_seconds: float) -> None:
        super().__init__(
            f"Link reports spend request {spend_request_id} as {status} but returned no "
            f"shared_payment_token after {waited_seconds:g} seconds."
        )
        self.spend_request_id = spend_request_id
        self.status = status


def retrieve_spend_request_shared_payment_token(
    spend_request_id: str,
    *,
    retry_timeout: float = 30.0,
    retry_interval: float = 1.0,
) -> str:
    """Return an SPT, tolerating brief propagation after spend approval."""
    deadline = time.monotonic() + max(0.0, retry_timeout)
    while True:
        sr = _retrieve_spend_request_with_shared_payment_token(spend_request_id)
        try:
            return _extract_shared_payment_token_from_spend_payload(sr)
        except RuntimeError as exc:
            status = sr.get("status", "unknown")
            if status not in _SPEND_SPT_RETRIEVE_OK:
                raise
            if time.monotonic() >= deadline:
                raise SharedPaymentTokenUnavailable(
                    spend_request_id, str(status), max(0.0, retry_timeout),
                ) from exc
            time.sleep(max(0.0, retry_interval))


def retrieve_spend_request(spend_request_id: str) -> SpendRequestStatus:
    """Poll spend request status (no card credentials)."""
    completed = subprocess.run(
        _link_cli_cmd("spend-request", "retrieve", spend_request_id, "--format", "json"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip() or f"link-cli retrieve failed for {spend_request_id}"
        )
    payload = _parse_link_cli_json(completed.stdout)
    sr = payload[0] if isinstance(payload, list) else payload
    return SpendRequestStatus(
        id=sr.get("id", spend_request_id),
        status=sr.get("status", "unknown"),
        approval_url=sr.get("approval_url"),
        approved_at=sr.get("approved_at") or sr.get("approvedAt"),
        next_action=parse_next_action(sr),
    )


def wait_for_spend_approval(
    spend_request_id: str,
    *,
    deadline_ts: float,
    poll_interval: float = 2.0,
    surfaced_action_key: Optional[str] = None,
) -> SpendRequestStatus:
    """Poll until the spend request can grant a credential, or the deadline.

    A ``requires_action`` request raises :class:`SpendActionRequired` so the caller
    can show the buyer the action, unless it is an ``auto_resume`` action whose
    ``key`` equals *surfaced_action_key*: that one was already shown, so polling
    the same request continues until the buyer finishes it.
    """
    import time

    while time.time() < deadline_ts:
        sr = retrieve_spend_request(spend_request_id)
        if sr.status in _SPEND_SPT_RETRIEVE_OK:
            return sr
        if sr.status in _SPEND_TERMINAL:
            raise RuntimeError(f"spend request {sr.status}")
        if sr.status == SPEND_REQUIRES_ACTION:
            action = sr.next_action
            if action is None:
                raise RuntimeError(
                    "spend request requires action but Link returned no next_action"
                )
            if not (action.auto_resume and action.key(spend_request_id) == surfaced_action_key):
                raise SpendActionRequired(spend_request_id, action)
        time.sleep(poll_interval)
    raise TimeoutError("approval_timeout")


def _parse_link_cli_json(stdout: str) -> Any:
    return json.loads(stdout.strip())


def _parse_link_cli_json_objects(stdout: str) -> list[Dict[str, Any]]:
    """Parse one or more JSON values from link-cli stdout (polling may yield many)."""
    dec = json.JSONDecoder()
    s = stdout.strip()
    objs: list[Dict[str, Any]] = []
    idx = 0
    while idx < len(s):
        while idx < len(s) and s[idx] not in "{[":
            nl = s.find("\n", idx)
            if nl == -1:
                break
            idx = nl + 1
        if idx >= len(s):
            break
        obj, end = dec.raw_decode(s, idx)
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict):
                    objs.append(item)
        elif isinstance(obj, dict):
            objs.append(obj)
        idx = end
        while idx < len(s) and s[idx].isspace():
            idx += 1
    return objs


def _first_link_cli_object(stdout: str) -> Dict[str, Any]:
    objs = _parse_link_cli_json_objects(stdout)
    if not objs:
        raise RuntimeError("link-cli returned no JSON objects")
    return objs[0]


def _last_link_cli_object(stdout: str) -> Dict[str, Any]:
    objs = _parse_link_cli_json_objects(stdout)
    if not objs:
        raise RuntimeError("link-cli returned no JSON objects")
    return objs[-1]


def _auth_status_from_obj(obj: Dict[str, Any]) -> "AuthStatus":
    pending = obj.get("pending") or obj.get("verification_url")
    return AuthStatus(
        authenticated=bool(obj.get("authenticated")),
        verification_url=obj.get("verification_url"),
        phrase=obj.get("phrase"),
        message=obj.get("message") or obj.get("instruction"),
    )


@dataclass
class AuthStatus:
    authenticated: bool
    verification_url: Optional[str] = None
    phrase: Optional[str] = None
    message: Optional[str] = None


def get_auth_status() -> AuthStatus:
    """One-shot link-cli auth status (no polling)."""
    completed = subprocess.run(
        _link_cli_cmd("auth", "status", "--interval", "0", "--format", "json"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "link-cli auth status failed"
        )
    return _auth_status_from_obj(_first_link_cli_object(completed.stdout))


def initiate_auth_login(*, client_name: str = "booking-agent") -> AuthStatus:
    """Start device auth and return verification_url without blocking on approval."""
    completed = subprocess.run(
        _link_cli_cmd(
            "auth",
            "login",
            "--client-name",
            client_name,
            "--interval",
            "0",
            "--format",
            "json",
        ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "link-cli auth login failed"
        )
    obj = _first_link_cli_object(completed.stdout)
    if obj.get("authenticated"):
        return _auth_status_from_obj(obj)
    if not obj.get("verification_url"):
        raise RuntimeError("link-cli auth login did not return verification_url")
    return _auth_status_from_obj(obj)


def poll_auth_until_authenticated(
    *,
    poll_interval: float = 2.0,
    timeout: int = 300,
) -> AuthStatus:
    """Block polling link-cli auth status until authenticated or timeout."""
    completed = subprocess.run(
        _link_cli_cmd(
            "auth",
            "status",
            "--interval",
            str(poll_interval),
            "--timeout",
            str(timeout),
            "--format",
            "json",
        ),
        capture_output=True,
        text=True,
        timeout=timeout + 30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "link-cli auth status polling failed"
        )
    final = _last_link_cli_object(completed.stdout)
    status = _auth_status_from_obj(final)
    if not status.authenticated:
        raise TimeoutError("link_auth_timeout")
    return status


def logout() -> None:
    """Log out of the Link wallet via link-cli."""
    completed = subprocess.run(
        _link_cli_cmd("auth", "logout"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "link-cli auth logout failed"
        )


@dataclass
class SpendRequestCreateResult:
    id: str
    approval_url: Optional[str]
    raw: Dict[str, Any]
    command_summary: str
    status: Optional[str] = None
    next_action: Optional[SpendNextAction] = None


def is_test_network_id(network_id: Optional[str]) -> bool:
    """True for a Stripe test-mode business profile id (``profile_test_...``)."""
    return bool(network_id) and str(network_id).strip().startswith("profile_test_")


def create_spend_request(
    *,
    payment_method_id: str,
    amount: int,
    currency: str,
    merchant_name: str,
    merchant_url: str,
    context: str,
    line_item_label: str,
    credential_type: Optional[str] = None,
    network_id: Optional[str] = None,
) -> SpendRequestCreateResult:
    """Create a link-cli spend request and return parsed response."""
    cmd = [
        "spend-request",
        "create",
        "--payment-method-id",
        payment_method_id,
        "--amount",
        str(amount),
        "--currency",
        currency,
        "--context",
        context,
        "--line-item",
        f"name:{line_item_label},unit_amount:{amount},quantity:1",
        "--total",
        f"type:total,display_text:Total,amount:{amount}",
        "--format",
        "json",
    ]
    # link-cli rejects merchant_name/merchant_url for shared_payment_token spend requests.
    if credential_type != "shared_payment_token":
        cmd.extend(
            [
                "--merchant-name",
                merchant_name,
                "--merchant-url",
                merchant_url,
            ]
        )
    if credential_type:
        cmd.extend(["--credential-type", credential_type])
    if network_id:
        cmd.extend(["--network-id", network_id])
    # Link only issues a shared payment token for a test-mode business profile when the
    # spend request itself is test mode; otherwise it approves and never returns a token.
    if credential_type == "shared_payment_token" and is_test_network_id(network_id):
        cmd.append("--test")

    summary_parts = []
    for i, part in enumerate(cmd):
        if part in ("--context",) and i + 1 < len(cmd):
            summary_parts.append(f'{part} "<redacted>"')
            continue
        summary_parts.append(part)
    command_summary = "link-cli " + " ".join(summary_parts)

    completed = subprocess.run(
        _link_cli_cmd(*cmd),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or "link-cli spend-request create failed"
        )
    payload = _parse_link_cli_json(completed.stdout)
    sr = payload[0] if isinstance(payload, list) else payload
    if not isinstance(sr, dict):
        raise RuntimeError("link-cli spend-request create returned non-object JSON")
    sr_id = str(sr.get("id") or "")
    if not sr_id:
        raise RuntimeError("link-cli spend-request create did not return an id")
    return SpendRequestCreateResult(
        id=sr_id,
        approval_url=sr.get("approval_url"),
        raw=sr,
        command_summary=command_summary,
        status=sr.get("status"),
        next_action=parse_next_action(sr),
    )
