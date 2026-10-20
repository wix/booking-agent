"""Minimal UCP checkout + profile HTTP server for E2E tests."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .pop_business import PopBusiness, ProofRejected
from .ucp_checkout_store import UcpCheckoutStore

MOCK_UCP_PORT = 8199


def minimal_ucp_profile(
    *, checkout_base: str, usp_base: str, pay_at_service: bool = False,
) -> Dict[str, Any]:
    doc = _minimal_ucp_profile(checkout_base=checkout_base, usp_base=usp_base)
    if pay_at_service:
        doc["ucp"]["capabilities"]["dev.usp-protocol.services.pay_at_service"] = [
            {"version": "2026-08-20", "extends": "dev.ucp.shopping.checkout"}
        ]
    return doc


def _minimal_ucp_profile(*, checkout_base: str, usp_base: str) -> Dict[str, Any]:
    return {
        "ucp": {
            "version": "2026-04-08",
            "capabilities": {
                "dev.ucp.shopping.checkout": [{"version": "2026-04-08"}],
                "dev.ucp.shopping.order": [{"version": "2026-08-25"}],
                "dev.ucp.common.payment.terms": [
                    {
                        "version": "2026-08-25",
                        "extends": ["dev.ucp.shopping.checkout", "dev.ucp.shopping.order"],
                    }
                ],
                "dev.usp-protocol.services.paid_bookings": [
                    {
                        "version": "2026-02-09",
                        "extends": "dev.ucp.shopping.checkout",
                    }
                ],
                "dev.usp-protocol.services.catalog": [{"version": "2026-02-09"}],
                "dev.usp-protocol.services.availability": [{"version": "2026-02-09"}],
            },
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": "2026-04-08",
                        "transport": "rest",
                        "endpoint": checkout_base,
                    }
                ],
                "dev.usp-protocol.services": [
                    {
                        "version": "2026-02-09",
                        "transport": "rest",
                        "endpoint": usp_base,
                    }
                ],
            },
            "payment_handlers": {
                "com.stripe.payments": [
                    {
                        "id": "stripe_payments",
                        "version": "2026-06-25",
                        "spec": "https://docs.stripe.com/agentic-commerce/ucp/stripe-payments-handler",
                        "schema": "https://ucp.stripe.com/payments/2026-06-25/schema.json",
                        "available_instruments": [
                            {
                                "type": "link",
                                "config": {"network_id": "profile_demo_not_for_production"},
                            }
                        ],
                    }
                ]
            },
        },
        "business": {
            "name": "Mock UCP Merchant",
            "currency": "USD",
            "timezone": "America/Phoenix",
        },
    }


def minimal_platform_profile() -> Dict[str, Any]:
    # Canonical doc lives in the production server module to avoid drift.
    from booking_agent.platform_profile import platform_profile_doc

    return platform_profile_doc()


class _UcpHandler(BaseHTTPRequestHandler):
    profile_doc: Dict[str, Any] = {}
    platform_doc: Dict[str, Any] = {}
    checkout_store: Optional[UcpCheckoutStore] = None
    requests: List[Tuple[str, str]] = []
    pop_business: Optional[PopBusiness] = None

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        pass

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length).decode()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proof_accepted(self, method: str) -> bool:
        """Enforce ``platform_key_pop`` on checkout the way a business does.

        Checkout is privileged, so an unproven request is a 401 here exactly as
        it is on a live business.  There is no credential at checkout, hence the
        issuance shape: proof only, no ``Authorization``.
        """
        business = self.pop_business
        if business is None:
            return True
        url = f"http://127.0.0.1:{self.server.server_port}{urlparse(self.path).path}"
        try:
            business.verify(
                method=method,
                url=url,
                authorization=None,
                dpop=self.headers.get("DPoP"),
                agent_profile=self.headers.get("UCP-Agent"),
            )
        except ProofRejected as rejected:
            code, payload = rejected.to_response()
            self._send_json(code, payload)
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        self.requests.append(("GET", path))
        if path.endswith("/.well-known/ucp"):
            self._send_json(200, self.profile_doc)
            return
        if path.endswith("/platform-profile"):
            self._send_json(200, self.platform_doc)
            return
        if "/checkout-sessions/" in path:
            if not self._proof_accepted("GET"):
                return
            cid = path.rsplit("/", 1)[-1]
            if not self.checkout_store:
                self.send_error(500)
                return
            session = self.checkout_store.get_checkout(cid)
            if not session:
                self.send_error(404)
                return
            self._send_json(200, session)
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        self.requests.append(("POST", path))
        if path.endswith("/checkout-sessions"):
            body = self._read_json()
            if not self._proof_accepted("POST"):
                return
            if not self.checkout_store:
                self.send_error(500)
                return
            session = self.checkout_store.create_checkout(body)
            self._send_json(200, session)
            return
        if "/checkout-sessions/" in path and path.endswith("/complete"):
            complete_body = self._read_json()
            if not self._proof_accepted("POST"):
                return
            cid = path.rsplit("/", 2)[-2]
            if not self.checkout_store:
                self.send_error(500)
                return
            status, payload = self.checkout_store.complete_checkout_checked(cid, complete_body)
            if payload is None:
                self.send_error(404)
                return
            self._send_json(status, payload)
            return
        self.send_error(404)


class MockUcpServer:
    def __init__(
        self,
        port: int = MOCK_UCP_PORT,
        *,
        checkout_base: Optional[str] = None,
        usp_base: str = "https://kobym9.wixsite.com/hvac-koby",
    ) -> None:
        self.port = port
        base = checkout_base or f"http://127.0.0.1:{port}/ucp/v1"
        self.base_url = f"http://127.0.0.1:{port}"
        self.profile_url = f"{self.base_url}/.well-known/ucp"
        self.platform_profile_url = f"{self.base_url}/platform-profile"
        self.checkout_base = base
        profile_doc = minimal_ucp_profile(
            checkout_base=base, usp_base=usp_base.rstrip("/"),
        )
        _UcpHandler.profile_doc = profile_doc
        _UcpHandler.platform_doc = minimal_platform_profile()
        _UcpHandler.checkout_store = UcpCheckoutStore(profile_doc=profile_doc)
        # This merchant accepts platform_key_pop, so checkout without a proof
        # must fail here as it does live.
        self.pop_business = PopBusiness(audience=self.profile_url)
        _UcpHandler.pop_business = self.pop_business
        self.requests: List[Tuple[str, str]] = []
        _UcpHandler.requests = self.requests
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._server = HTTPServer(("127.0.0.1", self.port), _UcpHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
