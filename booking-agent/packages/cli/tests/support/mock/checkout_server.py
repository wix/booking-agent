"""Minimal HTTP checkout server for CLI integration tests."""
from __future__ import annotations

import html
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .backend import MOCK_CHECKOUT_PORT

_PAID: set[str] = set()
# Optional post-payment redirect base. When set, the POST handler responds 302
# to "{base}/usp/return?booking_session=..." instead of an inline 200 page,
# simulating a merchant that honors the USP PostPaymentReturnRequest.
_RETURN_REDIRECT: dict[str, str] = {}


def mark_booking_paid(booking_id: str) -> None:
    _PAID.add(booking_id)


def is_booking_paid(booking_id: str) -> bool:
    return booking_id in _PAID


def reset_paid() -> None:
    _PAID.clear()


def set_return_redirect(base_url: Optional[str], booking_session: Optional[str] = None) -> None:
    """Configure (or clear) a post-payment 302 redirect to a PPRR return URL."""
    _RETURN_REDIRECT.clear()
    if base_url:
        _RETURN_REDIRECT["base"] = base_url.rstrip("/")
        if booking_session:
            _RETURN_REDIRECT["booking_session"] = booking_session


class _CheckoutHandler(BaseHTTPRequestHandler):
    on_paid: Optional[Callable[[str], None]] = None

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/pay/"):
            booking_id = parsed.path.split("/pay/", 1)[1].split("/", 1)[0]
            self._serve_checkout_form(booking_id)
            return
        if parsed.path.startswith("/session/"):
            booking_id = parsed.path.split("/session/", 1)[1].split("/", 1)[0]
            redirect = f"http://127.0.0.1:{MOCK_CHECKOUT_PORT}/pay/{booking_id}"
            self.send_response(302)
            self.send_header("Location", redirect)
            self.end_headers()
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/pay/"):
            booking_id = parsed.path.split("/pay/", 1)[1].split("/", 1)[0]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode() if length else ""
            params = parse_qs(body)
            card = (params.get("cardnumber") or [""])[0]
            if not card:
                self.send_error(400, "missing card")
                return
            mark_booking_paid(booking_id)
            if self.on_paid:
                self.on_paid(booking_id)
            if _RETURN_REDIRECT.get("base"):
                bs = _RETURN_REDIRECT.get("booking_session", booking_id)
                location = f"{_RETURN_REDIRECT['base']}/usp/return?booking_session={bs}"
                self.send_response(302)
                self.send_header("Location", location)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Payment complete</h1></body></html>")
            return
        self.send_error(404)

    def _serve_checkout_form(self, booking_id: str) -> None:
        bid = html.escape(booking_id)
        page = f"""<!DOCTYPE html>
<html><body>
<h1>Checkout</h1>
<form method="POST" action="/pay/{bid}">
<label>Email <input type="email" name="email" required></label><br>
<label>First name <input name="first_name" placeholder="First name" required></label><br>
<label>Last name <input name="last_name" placeholder="Last name" required></label><br>
<label>Phone <input type="tel" name="phone"></label><br>
<button type="button" onclick="document.getElementById('paystep').style.display='block'">Continue</button>
<div id="paystep" style="display:none">
<label>Card <input name="cardnumber" autocomplete="cc-number" required></label><br>
<label>Exp <input name="exp-date" autocomplete="cc-exp" placeholder="MM/YY" required></label><br>
<label>CVC <input name="cvc" autocomplete="cc-csc" required></label><br>
<button type="submit">Place Order</button>
</div>
</form>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(page.encode())


class MockCheckoutServer:
    def __init__(self, port: int = MOCK_CHECKOUT_PORT) -> None:
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        reset_paid()
        _RETURN_REDIRECT.clear()
        handler = type("Handler", (_CheckoutHandler,), {})
        self._server = HTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    @property
    def session_url_template(self) -> str:
        return f"http://127.0.0.1:{self.port}/session/{{booking_id}}"
