"""Shared in-memory UCP checkout session store for mock HTTP servers."""
from __future__ import annotations

import copy
import os
import uuid
from typing import Any, Dict, Optional

_STRIPE_PAYMENTS_NS = "com.stripe.payments"
_STRIPE_PAYMENTS_HANDLER_ID = "stripe_payments"


def checkout_totals(*, amount: int = 5000, currency: str = "USD") -> list:
    """UCP checkout ``totals`` is an array of ``{type, amount}`` entries, not an object."""
    return [
        {"type": "subtotal", "amount": amount},
        {"type": "total", "amount": amount},
    ]


def checkout_links() -> list:
    return [{"type": "terms", "url": "https://example.com/terms"}]


class UcpCheckoutStore:
    """Create, fetch, and complete UCP checkout sessions."""

    def __init__(
        self,
        *,
        profile_doc: Dict[str, Any],
        spt_network_id: Optional[str] = None,
        pay_at_service: bool = False,
    ) -> None:
        self.profile_doc = profile_doc
        #: Answer every booking checkout as a pay-at-service one (USP 11.3).
        self.pay_at_service = pay_at_service
        #: Every body POSTed to complete, in order, so tests can see what was sent.
        self.complete_bodies: list = []
        #: False answers complete like production pay-at-service: the order exists and the
        #: booking is still pending until a later booking read shows it confirmed.
        self.confirm_on_complete = True
        env_nid = (os.environ.get("BOOKING_AGENT_UCP_TEST_NETWORK_ID") or "").strip()
        self.spt_network_id = (spt_network_id or env_nid or None)
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def _checkout_payment_handlers(self) -> Dict[str, Any]:
        raw = self.profile_doc.get("ucp", {}).get("payment_handlers", {})
        if not self.spt_network_id or not isinstance(raw, dict):
            return raw if isinstance(raw, dict) else {}
        handlers = copy.deepcopy(raw)
        entries = handlers.get(_STRIPE_PAYMENTS_NS)
        if isinstance(entries, list):
            for h in entries:
                if not isinstance(h, dict):
                    continue
                ain = h.get("available_instruments")
                if not isinstance(ain, list):
                    continue
                for inst in ain:
                    if not isinstance(inst, dict):
                        continue
                    if inst.get("type") != "link":
                        continue
                    cfg = inst.get("config") if isinstance(inst.get("config"), dict) else {}
                    inst["config"] = {**cfg, "network_id": self.spt_network_id}
        return handlers

    def _checkout_capabilities(self) -> Dict[str, Any]:
        """Echo the capabilities active for this checkout (GH-123).

        A real business resolves and echoes only the capabilities relevant to the
        checkout (the booking + its checkout parent), not its whole catalog. We
        mirror that here so the platform can persist the authoritative set.
        """
        caps = self.profile_doc.get("ucp", {}).get("capabilities", {})
        if not isinstance(caps, dict):
            return {}
        active = (
            "dev.ucp.shopping.checkout",
            "dev.ucp.shopping.order",
            "dev.usp-protocol.services.paid_bookings",
            "dev.usp-protocol.services.pay_at_service",
            "dev.ucp.common.payment.terms",
        )
        return {ns: copy.deepcopy(caps[ns]) for ns in active if ns in caps}

    def create_checkout(self, body: Dict[str, Any]) -> Dict[str, Any]:
        cid = f"cs_{uuid.uuid4().hex[:12]}"
        booking = body.get("booking") if isinstance(body.get("booking"), dict) else {}
        bid = f"bkg_{uuid.uuid4().hex[:12]}"
        amount = _amount_from_body(body)
        currency = str(body.get("currency") or "USD")
        session = {
            "id": cid,
            "status": "ready_for_complete",
            "currency": currency,
            "totals": checkout_totals(amount=amount, currency=currency),
            "links": checkout_links(),
            "booking": {
                **booking,
                "booking_id": bid,
                "booking_status": "pending",
            },
            "ucp": {
                "version": self.profile_doc.get("ucp", {}).get("version"),
                "capabilities": self._checkout_capabilities(),
                "payment_handlers": self._checkout_payment_handlers(),
            },
            "payment": {
                "instruments": [
                    {
                        "id": "instr_demo",
                        "handler_id": _STRIPE_PAYMENTS_HANDLER_ID,
                        "type": "link",
                        "selected": True,
                    }
                ]
            },
        }
        service_id = booking.get("service_id")
        if service_id == "f2c6ffc0-ac-install-deposit":
            slot = booking.get("slot") if isinstance(booking.get("slot"), dict) else {}
            start = slot.get("start") or "2026-10-14T10:00:00-04:00"
            deposit = max(1, amount // 10)
            if deposit >= amount:
                amount = deposit + 1
                session["totals"] = checkout_totals(amount=amount, currency=currency)
            term = {
                "id": "pt_fixed_deposit",
                "title": "Deposit now, balance at the appointment",
                "schedules": [
                    {
                        "id": "sched_deposit",
                        "type": "immediate",
                        "description": {"plain": f"{deposit} due now"},
                        "amount": deposit,
                    },
                    {
                        "id": "sched_balance",
                        "type": "at_service",
                        "description": {"plain": f"{amount - deposit} due at the appointment"},
                        "due_at": start,
                        "amount": amount - deposit,
                    },
                ],
            }
            session["payment"]["selected_term_id"] = term["id"]
            session["payment"]["terms"] = [term]
            session["messages"] = [
                {
                    "presentation": "disclosure",
                    "code": "balance_due_at_service",
                    "description": {"plain": "The remaining balance is due at the appointment."},
                }
            ]
        if self.pay_at_service:
            self._make_pay_at_service(session, booking, amount)
        self.sessions[cid] = session
        return session

    @staticmethod
    def _make_pay_at_service(
        session: Dict[str, Any], booking: Dict[str, Any], amount: int,
    ) -> None:
        """Shape the checkout the way a business declaring pay_at_service answers it."""
        slot = booking.get("slot") if isinstance(booking.get("slot"), dict) else {}
        start = slot.get("start") or "2026-10-14T10:00:00-04:00"
        session["ucp"]["payment_handlers"] = {}
        session["payment"] = {
            "selected_term_id": "pt_at_service",
            "terms": [
                {
                    "id": "pt_at_service",
                    "title": "Pay at your appointment",
                    "schedules": [
                        {
                            "id": "sched_at_service",
                            "type": "at_service",
                            "description": {"plain": f"Due at your appointment on {start}."},
                            "due_at": start,
                            "amount": amount,
                        }
                    ],
                }
            ],
        }
        session["settlement"] = {
            "mode": "offline",
            "term_id": "pt_at_service",
            "accepted_methods": ["cash", "card_in_person"],
        }
        session["messages"] = [
            {
                "type": "warning",
                "code": "payment_due_at_service",
                "content": (
                    f"Nothing is charged for this booking now. The full price of "
                    f"{amount / 100:.2f} USD is due at your appointment on {start}."
                ),
                "path": "$.payment.selected_term_id",
                "presentation": "disclosure",
            }
        ]

    def get_checkout(self, checkout_id: str) -> Optional[Dict[str, Any]]:
        return self.sessions.get(checkout_id)

    def complete_checkout_checked(
        self, checkout_id: str, body: Optional[Dict[str, Any]],
    ) -> tuple[int, Optional[Dict[str, Any]]]:
        """Complete like a business: S5 rejects instruments on an offline-settled checkout."""
        self.complete_bodies.append(body)
        session = self.sessions.get(checkout_id)
        if not session:
            return 404, None
        payment = (body or {}).get("payment") if isinstance(body, dict) else None
        instruments = payment.get("instruments") if isinstance(payment, dict) else None
        settlement = session.get("settlement")
        if isinstance(settlement, dict) and settlement.get("mode") == "offline" and instruments:
            return 400, {
                "code": "validation_error",
                "message": (
                    "This booking is settled at the appointment, so no payment instrument "
                    "may be submitted for it. Complete the checkout with no instruments."
                ),
            }
        return 200, self.complete_checkout(checkout_id)

    def complete_checkout(self, checkout_id: str) -> Optional[Dict[str, Any]]:
        session = self.sessions.get(checkout_id)
        if not session:
            return None
        booking = session.get("booking") if isinstance(session.get("booking"), dict) else {}
        payment = session.get("payment") if isinstance(session.get("payment"), dict) else {}
        terms = payment.get("terms") if isinstance(payment.get("terms"), list) else []
        selected = next((t for t in terms if t.get("id") == payment.get("selected_term_id")), None)
        order_id = f"ord_{uuid.uuid4().hex[:12]}"
        completed = {
            **session,
            "status": "completed",
            "order_id": order_id,
            "booking": {
                **booking,
                "booking_status": "confirmed" if self.confirm_on_complete else "pending",
            },
        }
        if selected is not None:
            completed["order"] = {
                "id": order_id,
                "checkout_id": session.get("id"),
                "permalink_url": f"https://example.com/orders/{order_id}",
                "currency": session.get("currency") or "USD",
                "totals": session.get("totals") or [],
                "line_items": [],
                "fulfillment": {},
            }
        self.sessions[checkout_id] = completed
        return completed


def _amount_from_body(body: Dict[str, Any]) -> int:
    line_items = body.get("line_items")
    if isinstance(line_items, list) and line_items:
        first = line_items[0]
        if isinstance(first, dict):
            item = first.get("item")
            if isinstance(item, dict):
                price = item.get("price")
                if isinstance(price, int) and price > 0:
                    return price
            for key in ("amount", "price", "total"):
                val = first.get(key)
                if isinstance(val, int) and val > 0:
                    return val
    totals = body.get("totals")
    if isinstance(totals, dict):
        total = totals.get("total")
        if isinstance(total, int) and total > 0:
            return total
    return 5000
