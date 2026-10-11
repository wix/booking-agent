"""Optional local HTTP server for the UCP platform profile (offline demos only).

Production UCP-native checkout fetches the configured public profile and does
not use this server. The module is kept for offline mock stacks and manual
debugging only. Its loopback URL is non-conformant under RFC 6890 and the
DNS-rebinding checks required of production businesses::

    python -m booking_agent_cli.platform_profile_server --port 8097
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import urlparse

from booking_agent.platform_profile import platform_profile_doc

PLATFORM_PROFILE_PATH = "/platform/.well-known/ucp"


class _PlatformProfileHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path.endswith("/.well-known/ucp"):
            body = json.dumps(platform_profile_doc()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


def serve(port: int) -> None:
    """Serve the platform profile on 127.0.0.1:<port> until killed."""
    server = HTTPServer(("127.0.0.1", port), _PlatformProfileHandler)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="UCP platform profile server (offline demo)")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    serve(args.port)


if __name__ == "__main__":
    main()
