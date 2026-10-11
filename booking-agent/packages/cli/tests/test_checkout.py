"""Unit tests for checkout form-fill helpers."""
from unittest.mock import MagicMock

import pytest

from booking_agent_cli.checkout import (
    CardCredential,
    _assist_checkout_path,
    _country_select_candidates,
    _fill_select_locator,
    _page_has_validation_errors,
    _run_post_contact_checkout,
    _state_select_candidates,
    print_submit_only_handoff,
    print_virtual_card_handoff,
)
from booking_agent_cli.browser import BrowserLaunchSpec
from booking_agent_cli.checkout import CheckoutInput
from booking_agent_cli.session import Buyer


def test_state_select_candidates_abbreviation_expands():
    assert _state_select_candidates("AZ") == ["Arizona", "AZ"]


def test_state_select_candidates_full_name_includes_abbreviation():
    assert _state_select_candidates("Arizona") == ["Arizona", "AZ"]


def test_state_select_candidates_unknown_passthrough():
    assert _state_select_candidates("Ontario") == ["Ontario"]


def test_country_select_candidates_us():
    candidates = _country_select_candidates("US")
    assert "United States" in candidates
    assert "US" in candidates


def test_page_has_validation_errors_detects_address_hint():
    page = MagicMock()
    page.inner_text.return_value = "Please enter an address to continue."
    page.locator.return_value.count.return_value = 0
    assert _page_has_validation_errors(page) is True


def test_page_has_validation_errors_clear_page():
    page = MagicMock()
    page.inner_text.return_value = "Secure Checkout"
    page.locator.return_value.count.return_value = 0
    assert _page_has_validation_errors(page) is False


def test_fill_select_locator_tries_label_then_value():
    loc = MagicMock()
    loc.select_option.side_effect = [Exception("no label"), None]
    assert _fill_select_locator(loc, ["Arizona", "AZ"]) is True
    assert loc.select_option.call_count == 2


def test_run_post_contact_checkout_fills_billing_after_reveal(monkeypatch):
    calls: list[str] = []

    def record(name):
        def _inner(*_args, **_kwargs):
            calls.append(name)
        return _inner

    monkeypatch.setattr(
        "booking_agent_cli.checkout._fill_contact_fields", record("contact")
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._fill_billing_address", record("billing")
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._advance_checkout_steps", record("advance")
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._ensure_form_complete", record("validate")
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._reveal_payment_section", record("reveal")
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._try_automated_card_fill", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._complete_checkout_submit", lambda *_a, **_k: True
    )

    page = MagicMock()
    inp = CheckoutInput(
        continue_url="https://example.com/checkout",
        buyer=Buyer(name="Ada Lovelace", email="ada@example.com", phone="+15551234567"),
        card=CardCredential(
            number="4242424242424242",
            exp_month=12,
            exp_year=2030,
            cvc="123",
            billing_line1="1 Main St",
            billing_city="Yuma",
            billing_state="AZ",
            billing_postal_code="85364",
        ),
        browser_launch=BrowserLaunchSpec(channel="chromium", source="playwright"),
    )

    assert _run_post_contact_checkout(page, inp) is True
    assert calls.index("reveal") < calls.index("billing")
    assert calls.count("billing") >= 2
    assert calls.count("validate") >= 2


def test_assist_checkout_path_submit_only_skips_card_handoff(capsys, monkeypatch):
    printed = {"pci": 0, "submit": 0}

    def fake_pci(*_args, **_kwargs):
        printed["pci"] += 1

    def fake_submit(*_args, **_kwargs):
        printed["submit"] += 1

    monkeypatch.setattr("booking_agent_cli.checkout.print_virtual_card_handoff", fake_pci)
    monkeypatch.setattr("booking_agent_cli.checkout.print_submit_only_handoff", fake_submit)
    monkeypatch.setattr(
        "booking_agent_cli.checkout._fill_contact_fields", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._fill_billing_address", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._wait_for_user_card_entry", lambda *_a, **_k: True
    )

    page = MagicMock()
    page.url = "https://example.com/checkout"
    inp = CheckoutInput(
        continue_url="https://example.com/checkout",
        buyer=Buyer(name="Ada Lovelace", email="ada@example.com"),
        card=CardCredential(
            number="4242424242424242",
            exp_month=12,
            exp_year=2030,
            cvc="123",
        ),
        browser_launch=BrowserLaunchSpec(channel="chromium", source="playwright"),
    )

    assert _assist_checkout_path(page, inp, card_filled=True) is True
    assert printed["submit"] == 1
    assert printed["pci"] == 0


def test_assist_checkout_path_pci_handoff_when_card_not_filled(monkeypatch):
    printed = {"pci": 0, "submit": 0}

    monkeypatch.setattr(
        "booking_agent_cli.checkout.print_virtual_card_handoff",
        lambda *_a, **_k: printed.__setitem__("pci", printed["pci"] + 1),
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout.print_submit_only_handoff",
        lambda **_k: printed.__setitem__("submit", printed["submit"] + 1),
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._fill_contact_fields", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._fill_billing_address", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "booking_agent_cli.checkout._wait_for_user_card_entry", lambda *_a, **_k: True
    )

    page = MagicMock()
    page.url = "https://example.com/checkout"
    inp = CheckoutInput(
        continue_url="https://example.com/checkout",
        buyer=Buyer(name="Ada Lovelace", email="ada@example.com"),
        card=CardCredential(
            number="4242424242424242",
            exp_month=12,
            exp_year=2030,
            cvc="123",
        ),
        browser_launch=BrowserLaunchSpec(channel="chromium", source="playwright"),
    )

    assert _assist_checkout_path(page, inp, card_filled=False) is True
    assert printed["pci"] == 1
    assert printed["submit"] == 0


def test_handoff_messages(capsys):
    card = CardCredential(
        number="4242424242424242",
        exp_month=12,
        exp_year=2030,
        cvc="123",
        billing_name="Ada Lovelace",
    )
    print_virtual_card_handoff(card)
    pci_out = capsys.readouterr().err
    assert "contact and billing prefilled" in pci_out

    print_submit_only_handoff()
    submit_out = capsys.readouterr().err
    assert "prefilled" in submit_out
    assert "Place Order" in submit_out
