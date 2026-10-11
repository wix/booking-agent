"""Local post-payment return (PPRR) listener.

A lightweight loopback HTTP server that the merchant's checkout can redirect the
buyer browser to after payment (USP PostPaymentReturnRequest). When the redirect
arrives, an event is set so the booking-confirmation wait can poll immediately
instead of sleeping the full interval.

Local/agent runs only: the listener binds 127.0.0.1 on an ephemeral port, so it
can only receive redirects from a browser on the same host (e.g. the Playwright
checkout session). Production return URLs that point at a remote host cannot
reach it; in that case callers fall back to plain booking polling.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, Optional
from urllib.parse import parse_qs, urlparse

RETURN_PATH = "/usp/return"

_SUCCESS_HTML = (
    b"<!DOCTYPE html><html><body><h1>Payment received</h1>"
    b"<p>You can close this window and return to your agent.</p></body></html>"
)


@dataclass
class ReturnListenerHandle:
    server: HTTPServer
    thread: threading.Thread
    event: threading.Event
    base_url: str
    params: Dict[str, str] = field(default_factory=dict)

    @property
    def return_url(self) -> str:
        return f"{self.base_url}{RETURN_PATH}"


def _make_handler(handle_event: threading.Event, captured: Dict[str, str]):
    class _ReturnHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            pass

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != RETURN_PATH:
                self.send_error(404)
                return
            # Capture correlation params (e.g. booking_session) for inspection.
            for key, values in parse_qs(parsed.query).items():
                if values:
                    captured[key] = values[0]
            handle_event.set()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_SUCCESS_HTML)

    return _ReturnHandler


# Process-local registry of active listeners keyed by base_url. The listener's
# threading objects cannot cross process boundaries, so this only helps when
# `book` and `pay checkout` run in the same process (in-process agent / tests).
_ACTIVE_LISTENERS: Dict[str, "ReturnListenerHandle"] = {}


def start_local_return_listener() -> ReturnListenerHandle:
    """Start a daemon HTTP server on 127.0.0.1:<ephemeral> and return its handle."""
    event = threading.Event()
    captured: Dict[str, str] = {}
    server = HTTPServer(("127.0.0.1", 0), _make_handler(event, captured))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    handle = ReturnListenerHandle(
        server=server,
        thread=thread,
        event=event,
        base_url=f"http://127.0.0.1:{port}",
        params=captured,
    )
    _ACTIVE_LISTENERS[handle.base_url] = handle
    return handle


def get_active_listener(base_url: Optional[str]) -> Optional[ReturnListenerHandle]:
    """Return the in-process listener for base_url, if one is running."""
    if not base_url:
        return None
    return _ACTIVE_LISTENERS.get(base_url)


def wait_for_return(handle: ReturnListenerHandle, timeout_sec: float) -> Optional[Dict[str, str]]:
    """Block up to timeout_sec for a redirect; return captured params or None."""
    if handle.event.wait(timeout=timeout_sec):
        return dict(handle.params)
    return None


def stop_listener(handle: Optional[ReturnListenerHandle]) -> None:
    if handle is None:
        return
    _ACTIVE_LISTENERS.pop(handle.base_url, None)
    try:
        handle.server.shutdown()
    except Exception:
        pass
    try:
        handle.server.server_close()
    except Exception:
        pass
