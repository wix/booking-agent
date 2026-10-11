"""Playwright redirect checkout automation."""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .browser import BrowserLaunchSpec
from .session import Buyer

logger = logging.getLogger(__name__)

CHECKOUT_TIMEOUT_MS = int(os.environ.get("BOOKING_AGENT_CHECKOUT_TIMEOUT", "90000"))
CARD_AUTO_PROBE_MS = int(os.environ.get("BOOKING_AGENT_CARD_AUTO_PROBE_MS", "15000"))
DEFAULT_ASSIST_TIMEOUT_MS = int(os.environ.get("BOOKING_AGENT_ASSIST_TIMEOUT_MS", "300000"))

_CHECKOUT_PATH_MARKERS = ("/checkout", "/pay/", "/payment")
_CARD_IFRAME_SELECTORS = [
    'iframe[title*="card" i]',
    'iframe[title*="secure" i]',
    'iframe[name*="card" i]',
    'iframe[src*="card-form" i]',
    'iframe[src*="cashier-pci" i]',
    'iframe[src*="stripe" i]',
    'iframe[src*="js.stripe.com" i]',
]
_CARD_NUMBER_SELECTORS = [
    'input[name="cardnumber"]',
    'input[name="cardNumber"]',
    'input[name="number"]',
    'input[autocomplete="cc-number"]',
    'input[placeholder*="card" i]',
    'input[aria-label*="card" i]',
]
_CARD_EXP_SELECTORS = [
    'input[name="exp-date"]',
    'input[name="expDate"]',
    'input[name="cardExpiration"]',
    'input[name="expiration"]',
    'input[autocomplete="cc-exp"]',
    'input[placeholder*="MM" i]',
    'input[aria-label*="expir" i]',
]
_CARD_CVC_SELECTORS = [
    'input[name="cvc"]',
    'input[name="cardCvc"]',
    'input[autocomplete="cc-csc"]',
    'input[placeholder*="CVC" i]',
    'input[placeholder*="CVV" i]',
    'input[aria-label*="CVC" i]',
    'input[aria-label*="CVV" i]',
]
_CONTINUE_BUTTON_RE = re.compile(
    r"\b(continue|next|proceed|save and continue|go to payment|continue to payment)\b",
    re.I,
)
_SUBMIT_BUTTON_RE = re.compile(
    r"(place order|pay now|complete order|complete purchase|submit payment|"
    r"confirm and pay|confirm payment|complete payment|secure checkout|"
    r"place order & pay|^pay\b)",
    re.I,
)
_EXCLUDE_BUTTON_RE = re.compile(
    r"(continue browsing|back to|sign in|log in|create account|cancel order|show more|promo code)",
    re.I,
)
_SUCCESS_TEXT_RE = re.compile(
    r"(thank you|order confirm|payment complete|booking confirm|"
    r"successfully paid|payment received)",
    re.I,
)

_VALIDATION_HINTS = (
    "valid answer",
    "enter a valid",
    "enter an address",
    "enter a city",
    "enter a zip",
    "enter a postal",
    "choose an option",
    "this field is required",
    "field is required",
    "required field",
)

_US_STATE_NAMES: Dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}

_CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class CardCredential:
    number: str
    exp_month: int
    exp_year: int
    cvc: str
    billing_name: str = ""
    billing_line1: str = ""
    billing_city: str = ""
    billing_state: str = ""
    billing_postal_code: str = ""
    billing_country: str = "US"

    @classmethod
    def from_link_cli_payload(cls, payload: Dict[str, Any]) -> "CardCredential":
        card = payload.get("card") or payload
        addr = card.get("billing_address") or {}
        return cls(
            number=str(card["number"]),
            exp_month=int(card["exp_month"]),
            exp_year=int(card["exp_year"]),
            cvc=str(card["cvc"]),
            billing_name=addr.get("name") or "",
            billing_line1=addr.get("line1") or "",
            billing_city=addr.get("city") or "",
            billing_state=addr.get("state") or "",
            billing_postal_code=addr.get("postal_code") or "",
            billing_country=addr.get("country") or "US",
        )


@dataclass
class CheckoutInput:
    continue_url: str
    buyer: Buyer
    card: CardCredential
    browser_launch: BrowserLaunchSpec
    headless: bool = False
    timeout_ms: int = CHECKOUT_TIMEOUT_MS
    auto_card: bool = True
    assist_timeout_ms: int = DEFAULT_ASSIST_TIMEOUT_MS
    card_auto_probe_ms: int = CARD_AUTO_PROBE_MS
    expires_at: Optional[str] = None
    expires_in_sec: Optional[int] = None
    return_url_prefix: Optional[str] = None  # navigating here counts as checkout success


class AssistTimeout(RuntimeError):
    """Raised when the user does not complete manual card entry/submit in time."""


def _is_target_closed(exc: BaseException) -> bool:
    """True if the exception is Playwright's TargetClosedError (browser/page closed)."""
    for cls in type(exc).__mro__:
        if cls.__name__ == "TargetClosedError":
            return True
    return False


@dataclass
class CheckoutResult:
    final_url: str
    submitted: bool
    error: Optional[str] = None
    title: str = ""
    assist_timeout: bool = False
    browser_closed: bool = False


def _split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "Buyer").split(" ", 1)
    first = parts[0] or "Buyer"
    last = parts[1] if len(parts) > 1 else "Unknown"
    return first, last


def _phone_digits(phone: Optional[str]) -> str:
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)[-10:]


def _format_us_phone(phone: Optional[str]) -> str:
    digits = _phone_digits(phone)
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return digits or (phone or "")


def _checkout_path(url: str) -> str:
    return urlparse(url).path.lower()


def _on_checkout_page(url: str) -> bool:
    path = _checkout_path(url)
    return any(marker in path for marker in _CHECKOUT_PATH_MARKERS)


def _iter_frames(page):
    return page.frames


def _count_interactive_fields(page) -> int:
    try:
        js_count = page.evaluate(
            """() => [...document.querySelectorAll(
                'input:not([type=hidden]), select, textarea'
            )].filter(el => el.offsetParent !== null && !el.disabled).length"""
        )
        if js_count:
            return int(js_count)
    except Exception:
        pass
    total = 0
    for frame in _iter_frames(page):
        for el in frame.locator('input:not([type="hidden"]), select, textarea').all():
            try:
                if el.is_visible():
                    total += 1
            except Exception:
                continue
    return total


def _locator_in_frames(page, selectors: List[str]):
    for frame in _iter_frames(page):
        for sel in selectors:
            loc = frame.locator(sel).first
            try:
                if loc.count():
                    yield frame, loc, sel
            except Exception:
                continue


def _has_card_field(page) -> bool:
    for iframe_sel in _CARD_IFRAME_SELECTORS:
        try:
            fl = page.frame_locator(iframe_sel)
            for sel in _CARD_NUMBER_SELECTORS:
                loc = fl.locator(sel).first
                if loc.count() and loc.is_visible():
                    return True
        except Exception:
            continue
    for _frame, loc, _sel in _locator_in_frames(page, _CARD_NUMBER_SELECTORS):
        try:
            if loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _fill_by_label(page, label: str, value: str) -> bool:
    if not value:
        return False
    for frame in _iter_frames(page):
        try:
            loc = frame.get_by_label(label, exact=False).first
            if loc.count():
                loc.fill(value, timeout=5000)
                return True
        except Exception:
            continue
    return False


def _fill_first_visible(page, selectors: List[str], value: str) -> bool:
    if not value:
        return False
    for _frame, loc, _sel in _locator_in_frames(page, selectors):
        try:
            if loc.is_visible():
                loc.fill(value, timeout=5000)
                return True
        except Exception:
            continue
    return False


def _fill_in_frames(page, selectors: List[str], value: str) -> bool:
    if not value:
        return False
    for _frame, loc, _sel in _locator_in_frames(page, selectors):
        try:
            if loc.is_visible():
                loc.fill(value, timeout=5000)
                return True
        except Exception:
            continue
    return False


def _fill_locator(loc, value: str, *, force: bool = False) -> bool:
    try:
        if force or loc.is_visible():
            loc.fill(value, timeout=5000, force=force)
            return True
    except Exception:
        pass
    try:
        loc.click(timeout=3000, force=True)
        loc.press_sequentially(value, delay=50)
        return True
    except Exception:
        return False


def _state_select_candidates(state: str) -> List[str]:
    """Return label/value candidates for a US state dropdown."""
    state = (state or "").strip()
    if not state:
        return []
    upper = state.upper()
    if len(upper) == 2 and upper in _US_STATE_NAMES:
        full = _US_STATE_NAMES[upper]
        return _dedupe_candidates(full, upper, state)
    for abbr, full in _US_STATE_NAMES.items():
        if full.lower() == state.lower():
            return _dedupe_candidates(full, abbr, state)
    return [state]


def _country_select_candidates(country: str) -> List[str]:
    code = (country or "US").strip().upper()
    if code in ("US", "USA"):
        return _dedupe_candidates(
            "United States",
            "United States of America",
            "US",
            "USA",
            country,
        )
    return _dedupe_candidates(country, code)


def _dedupe_candidates(*values: str) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _resolve_field_locator(
    page,
    labels: List[str],
    autocomplete: List[str],
) -> Any:
    for label in labels:
        for frame in _iter_frames(page):
            try:
                loc = frame.get_by_label(label, exact=False).first
                if loc.count():
                    return loc
            except Exception:
                continue
    for sel in autocomplete:
        for _frame, loc, _sel in _locator_in_frames(page, [sel]):
            try:
                if loc.count():
                    return loc
            except Exception:
                continue
    return None


def _fill_select_locator(loc, candidates: List[str]) -> bool:
    for candidate in candidates:
        if not candidate:
            continue
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        for kwargs in ({"label": candidate}, {"value": candidate}):
            try:
                loc.select_option(**kwargs)
                return True
            except Exception:
                continue
    return False


def _click_dropdown_option(page, candidates: List[str]) -> bool:
    """Pick an option from an open custom dropdown or listbox."""
    for candidate in candidates:
        if not candidate:
            continue
        for frame in _iter_frames(page):
            for role in ("option", "menuitem", "listitem"):
                try:
                    loc = frame.get_by_role(role, name=candidate, exact=False).first
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=5000)
                        return True
                except Exception:
                    continue
            try:
                loc = frame.locator(f'li:has-text("{candidate}")').first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=5000)
                    return True
            except Exception:
                continue
    return False


def _open_country_combobox(page) -> bool:
    """Open a custom country dropdown (Wix and similar merchants)."""
    country_labels = ["Country", "Country/Region", "Country or region"]
    for label in country_labels:
        for frame in _iter_frames(page):
            try:
                loc = frame.get_by_label(label, exact=False).first
                if not loc.count():
                    continue
                tag = loc.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    return False
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=5000)
                return True
            except Exception:
                continue
            try:
                label_el = frame.get_by_text(label, exact=False).first
                if not label_el.count():
                    continue
                combobox = label_el.locator(
                    "xpath=ancestor::*[.//*[@role='combobox' or @role='button']][1]"
                ).get_by_role("combobox").first
                if not combobox.count():
                    combobox = label_el.locator(
                        "xpath=ancestor::*[.//*[@role='button']][1]"
                    ).locator('[role="button"]').first
                if combobox.count():
                    combobox.scroll_into_view_if_needed(timeout=3000)
                    combobox.click(timeout=5000)
                    return True
            except Exception:
                continue
    for frame in _iter_frames(page):
        try:
            loc = frame.get_by_role("combobox", name=re.compile("country", re.I)).first
            if loc.count():
                loc.scroll_into_view_if_needed(timeout=3000)
                loc.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


def _fill_country_field(page, country: str) -> bool:
    """Fill country on native selects or custom combobox dropdowns."""
    candidates = _country_select_candidates(country)
    if _fill_field(
        page,
        labels=["Country", "Country/Region", "Country or region"],
        autocomplete=[
            'select[name*="country" i]',
            'select[autocomplete="country"]',
            'select[autocomplete="shipping country"]',
            'select[autocomplete="billing country"]',
        ],
        value=country,
        select_candidates=candidates,
    ):
        return True
    if not _open_country_combobox(page):
        return False
    page.wait_for_timeout(500)
    return _click_dropdown_option(page, candidates)


def _fill_field(
    page,
    *,
    labels: List[str],
    autocomplete: List[str],
    value: str,
    select_candidates: Optional[List[str]] = None,
) -> bool:
    if not value:
        return False
    loc = _resolve_field_locator(page, labels, autocomplete)
    if loc is None:
        return False
    try:
        tag = loc.evaluate("el => el.tagName.toLowerCase()")
    except Exception:
        tag = "input"
    if tag == "select":
        return _fill_select_locator(loc, select_candidates or [value])
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    return _fill_locator(loc, value)


def _scroll_billing_into_view(page) -> None:
    try:
        page.evaluate(
            "window.scrollTo(0, Math.max(document.body.scrollHeight, "
            "document.documentElement.scrollHeight))"
        )
    except Exception:
        pass
    page.wait_for_timeout(500)


def _page_has_validation_errors(page) -> bool:
    try:
        body = page.inner_text("body", timeout=2000).lower()
        if any(hint in body for hint in _VALIDATION_HINTS):
            return True
    except Exception:
        pass
    try:
        if page.locator(':invalid:not([type=hidden]), [aria-invalid="true"]').count():
            return True
    except Exception:
        pass
    return False


def _fill_via_native_setter(frame, selector: str, value: str) -> bool:
    """Fill React-controlled inputs inside PCI iframes when Playwright fill is blocked."""
    if not value:
        return False
    try:
        filled = frame.evaluate(
            """({selector, value}) => {
                const el = document.querySelector(selector);
                if (!el) return false;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return (el.value || '').replace(/\\s/g, '').length > 0;
            }""",
            {"selector": selector, "value": value},
        )
        return bool(filled)
    except Exception:
        return False


def _ensure_form_complete(
    page, buyer: Buyer, card: CardCredential, timeout_ms: int
) -> None:
    """Re-fill contact and billing when checkout shows inline validation errors."""
    for _ in range(3):
        if not _page_has_validation_errors(page):
            return
        _fill_contact_fields(page, buyer, card)
        _fill_billing_address(page, card)
        _advance_checkout_steps(page, min(timeout_ms, 15000))
        page.wait_for_timeout(1500)


def _fill_pci_card_in_frames(page, card: CardCredential) -> bool:
    exp = f"{card.exp_month:02d}/{str(card.exp_year)[-2:]}"
    holder = card.billing_name or ""
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        for fr in _iter_frames(page):
            url = fr.url.lower()
            if not any(token in url for token in ("card-form", "cashier-pci", "stripe", "js.stripe.com")):
                continue
            number_ok = (
                _fill_via_native_setter(fr, "#cardNumber", card.number)
                or _fill_via_native_setter(fr, 'input[name="cardNumber"]', card.number)
            )
            exp_ok = (
                _fill_via_native_setter(fr, "#cardExpiration", exp)
                or _fill_via_native_setter(fr, 'input[name="cardExpiration"]', exp)
            )
            cvc_ok = (
                _fill_via_native_setter(fr, "#cardCvv", card.cvc)
                or _fill_via_native_setter(fr, 'input[name="cardCvv"]', card.cvc)
            )
            if holder:
                _fill_via_native_setter(fr, "#cardHolderName", holder)
                _fill_via_native_setter(fr, 'input[name="cardHolderName"]', holder)
            if number_ok and exp_ok and cvc_ok:
                verified = fr.evaluate(
                    """() => {
                        const num = document.querySelector('#cardNumber')?.value || '';
                        const exp = document.querySelector('#cardExpiration')?.value || '';
                        const cvc = document.querySelector('#cardCvv')?.value || '';
                        return num.replace(/\\s/g, '').length >= 4
                            && exp.replace(/\\s/g, '').length >= 4
                            && cvc.length >= 3;
                    }"""
                )
                if verified:
                    return True
        page.wait_for_timeout(500)
    return False


def _select_card_payment_method(page) -> None:
    """Select a card-based payment radio/tab when the checkout offers multiple methods."""
    for selector in (
        'input[type="radio"][aria-label*="credit" i]',
        'input[type="radio"][aria-label*="debit" i]',
    ):
        try:
            loc = page.locator(selector).first
            if loc.count():
                loc.check(force=True)
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue
    for label in ("Credit/Debit Cards", "Credit card", "Debit card"):
        try:
            loc = page.get_by_label(label, exact=False).first
            if loc.count():
                loc.check(force=True)
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue


def _reveal_payment_section(page, timeout_ms: int = 20000) -> None:
    """Scroll to and wait for hosted card iframes (often below the fold)."""
    try:
        page.evaluate(
            "window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))"
        )
    except Exception:
        pass
    page.wait_for_timeout(1500)
    _select_card_payment_method(page)
    for iframe_sel in _CARD_IFRAME_SELECTORS:
        try:
            loc = page.locator(iframe_sel).first
            if loc.count():
                loc.scroll_into_view_if_needed(timeout=5000)
                break
        except Exception:
            continue
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if _has_card_field(page):
            return
        for fr in _iter_frames(page):
            url = fr.url.lower()
            if any(token in url for token in ("card-form", "cashier-pci", "stripe", "js.stripe.com")):
                for sel in _CARD_NUMBER_SELECTORS:
                    try:
                        if fr.locator(sel).count():
                            return
                    except Exception:
                        continue
        page.wait_for_timeout(500)


def _fill_in_card_iframes(page, selectors: List[str], value: str) -> bool:
    if not value:
        return False
    for iframe_sel in _CARD_IFRAME_SELECTORS:
        try:
            iframe = page.locator(iframe_sel).first
            if iframe.count():
                iframe.scroll_into_view_if_needed(timeout=3000)
            fl = page.frame_locator(iframe_sel)
            for sel in selectors:
                loc = fl.locator(sel).first
                if loc.count() and _fill_locator(loc, value, force=True):
                    return True
        except Exception:
            continue
    for fr in _iter_frames(page):
        url = fr.url.lower()
        if not any(token in url for token in ("card-form", "cashier-pci", "stripe", "js.stripe.com")):
            continue
        for sel in selectors:
            loc = fr.locator(sel).first
            if loc.count() and _fill_locator(loc, value, force=True):
                return True
    return False


def _button_text(element) -> str:
    try:
        text = (element.inner_text(timeout=1000) or "").strip()
        if text:
            return text
    except Exception:
        pass
    for attr in ("value", "aria-label", "title"):
        try:
            val = element.get_attribute(attr)
            if val:
                return val.strip()
        except Exception:
            continue
    return ""


def _click_matching_button(
    page,
    match_re: re.Pattern[str],
    *,
    exclude_re: Optional[re.Pattern[str]] = None,
) -> bool:
    for frame in _iter_frames(page):
        for sel in ('button', 'input[type="submit"]', '[role="button"]'):
            for btn in frame.locator(sel).all():
                try:
                    if not btn.is_visible() or btn.is_disabled():
                        continue
                    text = _button_text(btn)
                    if not text or (exclude_re and exclude_re.search(text)):
                        continue
                    if not match_re.search(text):
                        continue
                    btn.scroll_into_view_if_needed(timeout=3000)
                    btn.click(timeout=5000)
                    return True
                except Exception:
                    continue
    return False


def _wait_for_redirect_to_checkout(page, timeout_ms: int) -> None:
    try:
        page.wait_for_function(
            """() => {
                const path = location.pathname.toLowerCase();
                return path.includes('/checkout') || path.includes('/pay/');
            }""",
            timeout=min(timeout_ms, 60000),
        )
    except Exception:
        pass


def _reload_threshold_sec(expires_in_sec: Optional[int]) -> float:
    if expires_in_sec is not None and expires_in_sec <= 360:
        return 20.0
    return 30.0


def _wait_for_checkout_ready(
    page,
    timeout_ms: int,
    *,
    expires_in_sec: Optional[int] = None,
) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    reloaded = False
    reload_after = _reload_threshold_sec(expires_in_sec)
    while time.monotonic() < deadline:
        field_count = _count_interactive_fields(page)
        if field_count >= 2 or _has_card_field(page):
            return
        if field_count >= 1 and _on_checkout_page(page.url):
            try:
                body = page.inner_text("body", timeout=2000)[:2500].lower()
                if any(
                    token in body
                    for token in ("email", "payment", "order summary", "place order", "checkout")
                ):
                    return
            except Exception:
                pass
        elapsed = (timeout_ms / 1000) - (deadline - time.monotonic())
        if elapsed > reload_after and not reloaded and _on_checkout_page(page.url):
            reloaded = True
            try:
                logger.info("checkout: reloading stalled checkout page")
                page.reload(wait_until="commit")
                page.wait_for_timeout(5000)
                continue
            except Exception:
                pass
        page.wait_for_timeout(1000)
    raise TimeoutError("Checkout page did not become ready")


def _mask_pan(number: str) -> str:
    digits = re.sub(r"\D", "", number)
    if len(digits) >= 4:
        return f"****{digits[-4:]}"
    return "****"


def _format_expires_countdown(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    minutes, secs = divmod(seconds, 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def print_virtual_card_handoff(
    card: CardCredential,
    *,
    expires_in_sec: Optional[int] = None,
    expires_at: Optional[str] = None,
) -> None:
    """Print Link virtual card credentials to stderr for human PCI paste."""
    exp = f"{card.exp_month:02d}/{str(card.exp_year)[-2:]}"
    countdown = _format_expires_countdown(expires_in_sec)
    expiry_line = f"Checkout expires in {countdown}"
    if expires_at:
        expiry_line += f" ({expires_at})"
    lines = [
        "",
        "Link virtual card for this payment (paste into the merchant checkout form):",
        f"  Card number: {card.number}",
        f"  Expiration:  {exp}",
        f"  CVC:         {card.cvc}",
    ]
    if card.billing_name:
        lines.append(f"  Name:        {card.billing_name}")
    lines.extend([
        "",
        f"{expiry_line}. Browser is open with contact and billing prefilled.",
        "Paste the card into the payment section and click Place Order",
        "(or wait for auto-submit when the form validates).",
        "",
    ])
    message = "\n".join(lines)
    print(message, file=sys.stderr, flush=True)
    logger.info(
        "checkout: assist handoff for virtual card ending %s, expires in %s",
        _mask_pan(card.number),
        countdown,
    )


def print_submit_only_handoff(
    *,
    expires_in_sec: Optional[int] = None,
    expires_at: Optional[str] = None,
) -> None:
    """Tell the user the form is complete and only submit remains."""
    countdown = _format_expires_countdown(expires_in_sec)
    expiry_line = f"Checkout expires in {countdown}"
    if expires_at:
        expiry_line += f" ({expires_at})"
    lines = [
        "",
        "Checkout form is prefilled (contact, billing, and card).",
        f"{expiry_line}.",
        "Review the details in the browser and click Place Order to complete payment.",
        "",
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)
    logger.info("checkout: submit-only assist handoff, expires in %s", countdown)


def _pci_frame_present(page) -> bool:
    for fr in _iter_frames(page):
        url = fr.url.lower()
        if any(token in url for token in ("card-form", "cashier-pci", "stripe", "js.stripe.com")):
            return True
    return False


def _verify_automated_card_fill(page, card: CardCredential) -> bool:
    """Return True when automated fill appears accepted by the merchant form."""
    if _pci_frame_present(page):
        for fr in _iter_frames(page):
            url = fr.url.lower()
            if not any(token in url for token in ("card-form", "cashier-pci", "stripe", "js.stripe.com")):
                continue
            try:
                ok = fr.evaluate(
                    """() => {
                        const num = document.querySelector('#cardNumber, input[name="cardNumber"]');
                        const exp = document.querySelector('#cardExpiration, input[name="cardExpiration"]');
                        const cvc = document.querySelector('#cardCvv, input[name="cardCvv"]');
                        if (!num || !exp || !cvc) return false;
                        if (num.getAttribute('data-success') === 'true') return true;
                        const nv = (num.value || '').replace(/\\s/g, '');
                        const ev = (exp.value || '').replace(/\\s/g, '');
                        const cv = (cvc.value || '').replace(/\\s/g, '');
                        return nv.length >= 4 && ev.length >= 4 && cv.length >= 3
                            && num.getAttribute('data-error') !== 'true';
                    }"""
                )
                if ok:
                    return True
            except Exception:
                continue
        return False
    for sel in _CARD_NUMBER_SELECTORS:
        for _frame, loc, _ in _locator_in_frames(page, [sel]):
            try:
                if loc.is_visible() and len(loc.input_value().replace(" ", "")) >= 4:
                    return True
            except Exception:
                continue
    return False


def _try_automated_card_fill(page, card: CardCredential, probe_ms: int) -> bool:
    """Attempt card fill within a short budget; fail fast for assist fallback."""
    deadline = time.monotonic() + (probe_ms / 1000)
    while time.monotonic() < deadline:
        if _fill_card_fields(page, card) and _verify_automated_card_fill(page, card):
            return True
        page.wait_for_timeout(500)
    return False


def _wait_for_user_card_entry(page, start_url: str, timeout_ms: int) -> bool:
    """Wait for user to complete PCI entry or payment. Returns True if submitted."""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if _wait_for_submission_result(page, start_url, 1000):
            return True
        page.wait_for_timeout(500)
    return False


def _complete_checkout_submit(page, timeout_ms: int) -> bool:
    try:
        page.get_by_text("Secure Checkout", exact=False).first.wait_for(
            state="visible", timeout=5000
        )
    except Exception:
        pass
    start_url = page.url
    clicked = _click_submit(page)
    if clicked:
        return _wait_for_submission_result(page, start_url, min(timeout_ms, 45000))
    return _wait_for_submission_result(page, start_url, 3000)


def _assist_checkout_path(page, inp: CheckoutInput, *, card_filled: bool) -> bool:
    if card_filled:
        print_submit_only_handoff(
            expires_in_sec=inp.expires_in_sec,
            expires_at=inp.expires_at,
        )
    else:
        print_virtual_card_handoff(
            inp.card,
            expires_in_sec=inp.expires_in_sec,
            expires_at=inp.expires_at,
        )
    _fill_contact_fields(page, inp.buyer, inp.card)
    _fill_billing_address(page, inp.card)
    start_url = page.url
    if _wait_for_user_card_entry(page, start_url, inp.assist_timeout_ms):
        return True
    if _complete_checkout_submit(page, min(inp.timeout_ms, 45000)):
        return True
    raise AssistTimeout("User did not complete card entry in time")


def _run_post_contact_checkout(page, inp: CheckoutInput) -> bool:
    _fill_contact_fields(page, inp.buyer, inp.card)
    _advance_checkout_steps(page, inp.timeout_ms)
    _ensure_form_complete(page, inp.buyer, inp.card, inp.timeout_ms)
    _reveal_payment_section(page, min(inp.timeout_ms, 30000))
    _fill_billing_address(page, inp.card)

    card_filled = False
    if inp.auto_card:
        card_filled = _try_automated_card_fill(page, inp.card, inp.card_auto_probe_ms)

    _fill_billing_address(page, inp.card)
    _ensure_form_complete(page, inp.buyer, inp.card, inp.timeout_ms)

    if card_filled and _complete_checkout_submit(page, inp.timeout_ms):
        return True

    if card_filled:
        logger.info("checkout: auto submit failed, falling back to assist (submit only)")
    else:
        logger.info("checkout: auto card fill failed, falling back to assist (pci)")

    if _assist_checkout_path(page, inp, card_filled=card_filled):
        return True
    return _complete_checkout_submit(page, inp.timeout_ms)


def _advance_checkout_steps(page, timeout_ms: int, max_steps: int = 5) -> None:
    for _ in range(max_steps):
        if _has_card_field(page):
            return
        if not _click_matching_button(
            page, _CONTINUE_BUTTON_RE, exclude_re=_EXCLUDE_BUTTON_RE
        ):
            return
        page.wait_for_timeout(1500)
        try:
            _wait_for_checkout_ready(page, min(timeout_ms, 15000))
        except TimeoutError:
            if _has_card_field(page):
                return


def _fill_contact_fields(page, buyer: Buyer, card: CardCredential) -> None:
    first, last = _split_name(buyer.name or card.billing_name)
    email = buyer.email
    phone = _format_us_phone(buyer.phone)

    _fill_field(
        page,
        labels=["Email"],
        autocomplete=[
            'input[type="email"]',
            'input[name*="email" i]',
            'input[autocomplete="email"]',
            'input[aria-label*="email" i]',
        ],
        value=email,
    )
    _fill_field(
        page,
        labels=["First name"],
        autocomplete=[
            'input[name*="first" i]',
            'input[placeholder*="First" i]',
            'input[autocomplete="given-name"]',
            'input[aria-label*="first" i]',
        ],
        value=first,
    )
    _fill_field(
        page,
        labels=["Last name"],
        autocomplete=[
            'input[name*="last" i]',
            'input[placeholder*="Last" i]',
            'input[autocomplete="family-name"]',
            'input[aria-label*="last" i]',
        ],
        value=last,
    )
    if phone:
        _fill_field(
            page,
            labels=["Phone"],
            autocomplete=[
                # Phone-specific selectors first. The bare ``input[type="tel"]``
                # fallback is guarded so it can never match a card number, CVC,
                # expiry, or postal-code field — those are also ``type="tel"``
                # and would otherwise swallow the phone number (see card-fill).
                'input[autocomplete="tel"]',
                'input[autocomplete="tel-national"]',
                'input[name*="phone" i]',
                'input[id*="phone" i]',
                'input[aria-label*="phone" i]',
                'input[placeholder*="phone" i]',
                'input[type="tel"]'
                ':not([autocomplete^="cc-" i])'
                ':not([name*="card" i]):not([name*="number" i])'
                ':not([name*="cvc" i]):not([name*="cvv" i])'
                ':not([name*="exp" i])'
                ':not([name*="zip" i]):not([name*="postal" i])'
                ':not([id*="card" i]):not([id*="number" i])'
                ':not([id*="cvc" i]):not([id*="cvv" i]):not([id*="exp" i])'
                ':not([aria-label*="card" i]):not([aria-label*="cvc" i])'
                ':not([aria-label*="cvv" i]):not([aria-label*="expir" i])'
                ':not([placeholder*="card" i]):not([placeholder*="cvc" i])'
                ':not([placeholder*="cvv" i])',
            ],
            value=phone,
        )
        try:
            page.get_by_label("Phone", exact=False).first.press("Tab")
        except Exception:
            pass


def _fill_billing_address(page, card: CardCredential) -> None:
    _scroll_billing_into_view(page)

    _fill_field(
        page,
        labels=["Address", "Street address", "Street"],
        autocomplete=[
            'input[name*="address" i]',
            'input[autocomplete="address-line1"]',
            'input[autocomplete="shipping address-line1"]',
            'input[autocomplete="billing address-line1"]',
            'input[placeholder*="Address" i]',
            'input[aria-label*="address" i]',
        ],
        value=card.billing_line1,
    )
    _fill_field(
        page,
        labels=["City"],
        autocomplete=[
            'input[name*="city" i]',
            'input[autocomplete="address-level2"]',
            'input[autocomplete="shipping address-level2"]',
            'input[autocomplete="billing address-level2"]',
            'input[aria-label*="city" i]',
        ],
        value=card.billing_city,
    )
    _fill_field(
        page,
        labels=["State", "Province", "Region"],
        autocomplete=[
            'select[name*="state" i]',
            'select[autocomplete="address-level1"]',
            'select[autocomplete="shipping address-level1"]',
            'select[autocomplete="billing address-level1"]',
            'input[name*="state" i]',
            'input[autocomplete="address-level1"]',
            'input[autocomplete="shipping address-level1"]',
            'input[autocomplete="billing address-level1"]',
            'input[aria-label*="state" i]',
        ],
        value=card.billing_state,
        select_candidates=_state_select_candidates(card.billing_state),
    )
    _fill_field(
        page,
        labels=["Zip", "Postal code", "Zip / Postal code"],
        autocomplete=[
            'input[name*="zip" i]',
            'input[name*="postal" i]',
            'input[autocomplete="postal-code"]',
            'input[autocomplete="shipping postal-code"]',
            'input[autocomplete="billing postal-code"]',
            'input[aria-label*="zip" i]',
            'input[aria-label*="postal" i]',
        ],
        value=card.billing_postal_code,
    )
    if card.billing_country:
        _fill_country_field(page, card.billing_country)


def _fill_contact_and_billing(page, buyer: Buyer, card: CardCredential) -> None:
    _fill_contact_fields(page, buyer, card)
    _fill_billing_address(page, card)


def _fill_card_fields(page, card: CardCredential) -> bool:
    if any(
        "card-form" in fr.url or "cashier-pci" in fr.url
        for fr in _iter_frames(page)
    ):
        return _fill_pci_card_in_frames(page, card)
    exp = f"{card.exp_month:02d}/{str(card.exp_year)[-2:]}"
    holder = card.billing_name or ""
    number_ok = _fill_in_card_iframes(page, _CARD_NUMBER_SELECTORS, card.number)
    if not number_ok:
        number_ok = _fill_in_frames(page, _CARD_NUMBER_SELECTORS, card.number)
    exp_ok = _fill_in_card_iframes(page, _CARD_EXP_SELECTORS, exp)
    if not exp_ok:
        exp_ok = _fill_in_frames(page, _CARD_EXP_SELECTORS, exp)
    cvc_ok = _fill_in_card_iframes(page, _CARD_CVC_SELECTORS, card.cvc)
    if not cvc_ok:
        cvc_ok = _fill_in_frames(page, _CARD_CVC_SELECTORS, card.cvc)
    if holder:
        _fill_in_card_iframes(page, ['input[name="cardHolderName"]'], holder)
        _fill_in_frames(page, ['input[name="cardHolderName"]'], holder)
    if number_ok and exp_ok and cvc_ok:
        return True
    return _fill_pci_card_in_frames(page, card)


def _click_submit(page) -> bool:
    if _click_matching_button(page, _SUBMIT_BUTTON_RE, exclude_re=_EXCLUDE_BUTTON_RE):
        return True
    for frame in _iter_frames(page):
        try:
            loc = frame.get_by_role("button", name=_SUBMIT_BUTTON_RE).first
            if loc.count() and loc.is_visible() and not loc.is_disabled():
                loc.click(timeout=5000)
                return True
        except Exception:
            pass
        for label in ("Place Order & Pay", "Secure Checkout", "Place Order", "Pay Now", "Complete Order"):
            try:
                loc = frame.get_by_text(label, exact=False).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=5000)
                    return True
            except Exception:
                pass
    for frame in _iter_frames(page):
        for btn in frame.locator('input[type="submit"], button[type="submit"]').all():
            try:
                if btn.is_visible() and not btn.is_disabled():
                    text = _button_text(btn)
                    if text and _EXCLUDE_BUTTON_RE.search(text):
                        continue
                    btn.scroll_into_view_if_needed(timeout=3000)
                    btn.click(timeout=5000)
                    return True
            except Exception:
                continue
    return False


def _wait_for_submission_result(page, start_url: str, timeout_ms: int) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        try:
            body = page.inner_text("body", timeout=2000)[:3000]
            if _SUCCESS_TEXT_RE.search(body):
                return True
        except Exception:
            pass
        if page.url != start_url and not _on_checkout_page(page.url):
            return True
        page.wait_for_timeout(1000)
    return False


def _browser_launch_args(headless: bool) -> List[str]:
    return ["--disable-blink-features=AutomationControlled"]


def run_redirect_checkout(inp: CheckoutInput) -> CheckoutResult:
    """Navigate continue_url once, fill checkout, submit payment."""
    from playwright.sync_api import sync_playwright

    launch_kwargs = inp.browser_launch.playwright_launch_kwargs(headless=inp.headless)
    launch_kwargs.setdefault("args", [])
    launch_kwargs["args"] = list(launch_kwargs["args"]) + _browser_launch_args(inp.headless)

    submitted = False
    final_url = ""
    title = ""
    error: Optional[str] = None
    assist_timeout = False
    browser_closed = False

    try:
        with sync_playwright() as p:
            browser = None
            context = None
            try:
                browser = p.chromium.launch(**launch_kwargs)
                context = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=_CHROME_UA,
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.set_default_timeout(inp.timeout_ms)

                logger.info("checkout: navigating continue_url")
                page.goto(inp.continue_url, wait_until="commit")
                _wait_for_redirect_to_checkout(page, inp.timeout_ms)
                page.wait_for_timeout(3000)
                _wait_for_checkout_ready(
                    page, inp.timeout_ms, expires_in_sec=inp.expires_in_sec
                )

                submitted = _run_post_contact_checkout(page, inp)

                final_url = page.url
                title = page.title()
                # A redirect to the local PPRR return URL also signals success.
                if (
                    not submitted
                    and inp.return_url_prefix
                    and final_url.startswith(inp.return_url_prefix)
                ):
                    submitted = True
            except AssistTimeout as exc:
                assist_timeout = True
                error = str(exc)
                logger.warning("checkout assist timeout: %s", exc)
            except Exception as exc:
                if _is_target_closed(exc):
                    browser_closed = True
                    error = "browser was closed before checkout completed"
                    logger.warning("checkout: browser/page closed before completion")
                else:
                    error = str(exc)
                    logger.exception("checkout failed")
            finally:
                # Release resources on every path; ignore double-close on an
                # already-closed target (e.g. user closed the window).
                for closer in (context, browser):
                    if closer is not None:
                        try:
                            closer.close()
                        except Exception:
                            pass
    except Exception as exc:
        # sync_playwright() startup/teardown failure
        error = error or str(exc)
        logger.exception("checkout failed (playwright lifecycle)")

    return CheckoutResult(
        final_url=final_url,
        submitted=submitted,
        error=error,
        title=title,
        assist_timeout=assist_timeout,
        browser_closed=browser_closed,
    )
