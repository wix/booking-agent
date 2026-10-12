"""Minimal ``booking.confirmed`` webhook receiver (GH-057).

Uses stdlib HTTP server. When ``BOOKING_AGENT_WEBHOOK_VERIFY=1``, POST requests must
include a non-empty ``Signature`` header (stub until full RFC 9421 verification
against merchant signing keys).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple

from booking_agent import settings

logger = logging.getLogger(__name__)

_EVENT_IDS: set[str] = set()
_CONFIRMED: Dict[Tuple[str, str], Dict[str, Any]] = {}


def _remember_event(event_id: str) -> bool:
    """Return True if new, False if duplicate (idempotency)."""
    if event_id in _EVENT_IDS:
        return False
    _EVENT_IDS.add(event_id)
    return True


class _Handler(BaseHTTPRequestHandler):
    server_version = "booking-agent-webhook/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/webhooks/booking", "/"):
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if settings.get_bool("BOOKING_AGENT_WEBHOOK_VERIFY"):
            # Stub: real verification is RFC 9421 over the raw body and signing keys from
            # the merchant profile. Until wired, require a non-empty Signature header.
            sig = (self.headers.get("Signature") or self.headers.get("signature") or "").strip()
            if not sig:
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"missing_signature"}')
                return
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        event_id = str(payload.get("event_id") or payload.get("id") or "")
        if not event_id:
            event_id = str(uuid.uuid4())
        if not _remember_event(event_id):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"duplicate":true}')
            return
        booking_id = str(payload.get("booking_id") or payload.get("booking", {}).get("id") or "")
        order_id = str(payload.get("order_id") or payload.get("order", {}).get("id") or "")
        if booking_id and order_id:
            _CONFIRMED[(booking_id, order_id)] = payload
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


def start_webhook_server(host: str = "127.0.0.1", port: int = 0) -> Tuple[HTTPServer, str]:
    server = HTTPServer((host, port), _Handler)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/webhooks/booking"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, url


def stop_webhook_server(server: HTTPServer) -> None:
    server.shutdown()


async def wait_for_booking_confirmed(
    booking_id: str,
    order_id: str,
    *,
    timeout_sec: float = 30.0,
    poll_interval: float = 0.2,
) -> Optional[Dict[str, Any]]:
    deadline = time.time() + timeout_sec
    key = (booking_id, order_id)
    while time.time() < deadline:
        hit = _CONFIRMED.pop(key, None)
        if hit is not None:
            return hit
        await asyncio.sleep(poll_interval)
    return None
