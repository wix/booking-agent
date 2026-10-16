"""Multiplexed local HTTP stack for end-to-end tests: registry + USP REST + UCP checkout."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from booking_agent.models.platform import ServiceSearchResult, ServiceSearchResultPrice
from booking_agent.models.wire import RegistryBusinessInfo

from ..mock.backend import _build_mock_slots, _score_service
from ..fixtures import MOCK_SERVICES, SERVICE_BY_ID
from ..mock.ucp_checkout_store import UcpCheckoutStore
from ..mock.ucp_server import minimal_platform_profile, minimal_ucp_profile

_AC_REPAIR_ID = "e4d4b7a4-0e66-4104-891b-ba5bb4a04092"


def _duration_minutes(service) -> Optional[int]:
    fixed = service.duration.fixed if service.duration else None
    if not fixed:
        return None
    m = re.match(r"PT(\d+)M", fixed)
    if m:
        return int(m.group(1))
    h = re.match(r"PT(\d+)H", fixed)
    if h:
        return int(h.group(1)) * 60
    return None


def _registry_results(profile_url: str, query: str) -> List[ServiceSearchResult]:
    ranked = sorted(
        ((s, _score_service(s, query)) for s in MOCK_SERVICES),
        key=lambda pair: pair[1],
        reverse=True,
    )
    scored = [s for s, score in ranked if score > 0]
    if not scored:
        scored = list(MOCK_SERVICES)
    results: List[ServiceSearchResult] = []
    for svc in scored[:3]:
        biz = svc.business
        if not biz:
            continue
        price = None
        if svc.pricing and svc.pricing.amount is not None:
            price = ServiceSearchResultPrice(
                amount=svc.pricing.amount,
                currency=svc.pricing.currency or "USD",
            )
        results.append(
            ServiceSearchResult(
                service_id=svc.id,
                service_name=svc.name,
                business=RegistryBusinessInfo(
                    id=biz.id,
                    name=biz.name,
                    profile_url=profile_url,
                ),
                category=(
                    svc.category.name
                    if svc.category and svc.category.name
                    else "Services"
                ),
                duration_minutes=_duration_minutes(svc),
                price=price,
            )
        )
    return results


class _OfflineHandler(BaseHTTPRequestHandler):
    base_url: str = ""
    usp_base: str = ""
    checkout_base: str = ""
    profile_doc: Dict[str, Any] = {}
    platform_doc: Dict[str, Any] = {}
    checkout_store: Optional[UcpCheckoutStore] = None

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

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path.endswith("/registry/search_services"):
            self._send_json(200, {"status": "ok"})
            return
        if path.endswith("/.well-known/ucp"):
            self._send_json(200, self.profile_doc)
            return
        if path.endswith("/platform/.well-known/ucp"):
            self._send_json(200, self.platform_doc)
            return
        if "/checkout-sessions/" in path:
            cid = path.rsplit("/", 1)[-1]
            session = self.checkout_store.get_checkout(cid) if self.checkout_store else None
            if not session:
                self.send_error(404)
                return
            self._send_json(200, session)
            return
        if "/usp/v1/services/" in path:
            service_id = path.rsplit("/", 1)[-1]
            svc = SERVICE_BY_ID.get(service_id)
            if not svc:
                self.send_error(404)
                return
            self._send_json(200, {"service": svc.model_dump(by_alias=True, exclude_none=True)})
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path.endswith("/registry/search_services"):
            body = self._read_json()
            query = str(body.get("query") or "")
            results = _registry_results(f"{self.base_url}/.well-known/ucp", query)
            payload = {
                "results": [r.model_dump(by_alias=True, exclude_none=True) for r in results],
                "pagination": {"count": len(results)},
            }
            self._send_json(200, payload)
            return
        if path.endswith("/availability/query"):
            body = self._read_json()
            service_id = str(body.get("service_id") or "")
            start_date = str(body.get("start_date") or "")
            end_date = str(body.get("end_date") or "")
            svc = SERVICE_BY_ID.get(service_id)
            if not svc:
                self._send_json(200, {"slots": []})
                return
            slots = _build_mock_slots(svc, start_date, end_date)
            self._send_json(
                200,
                {
                    "slots": [
                        s.model_dump(by_alias=True, exclude_none=True) for s in slots
                    ]
                },
            )
            return
        if path.endswith("/checkout-sessions"):
            body = self._read_json()
            if not self.checkout_store:
                self.send_error(500)
                return
            session = self.checkout_store.create_checkout(body)
            self._send_json(200, session)
            return
        if "/checkout-sessions/" in path and path.endswith("/complete"):
            cid = path.rsplit("/", 2)[-2]
            if not self.checkout_store:
                self.send_error(500)
                return
            completed = self.checkout_store.complete_checkout(cid)
            if not completed:
                self.send_error(404)
                return
            self._send_json(200, completed)
            return
        self.send_error(404)


class OfflineHttpStack:
    """Registry + USP REST + UCP on one loopback port."""

    def __init__(self, port: int = 0) -> None:
        self._requested_port = port
        self.port = port
        self.base_url = ""
        self.registry_url = ""
        self.profile_url = ""
        self.platform_profile_url = ""
        self.usp_base = ""
        self.checkout_base = ""
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def _configure_handler(self) -> None:
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.registry_url = f"{self.base_url}/registry/search_services"
        self.profile_url = f"{self.base_url}/.well-known/ucp"
        self.platform_profile_url = f"{self.base_url}/platform/.well-known/ucp"
        self.usp_base = f"{self.base_url}/usp/v1"
        self.checkout_base = f"{self.base_url}/ucp/v1"
        profile_doc = minimal_ucp_profile(
            checkout_base=self.checkout_base,
            usp_base=self.usp_base,
        )
        _OfflineHandler.base_url = self.base_url
        _OfflineHandler.usp_base = self.usp_base
        _OfflineHandler.checkout_base = self.checkout_base
        _OfflineHandler.profile_doc = profile_doc
        _OfflineHandler.platform_doc = minimal_platform_profile()
        _OfflineHandler.checkout_store = UcpCheckoutStore(profile_doc=profile_doc)

    def start(self) -> Tuple[int, str]:
        """Bind, serve in a daemon thread, return (port, base_url)."""
        self._server = HTTPServer(("127.0.0.1", self._requested_port), _OfflineHandler)
        self.port = self._server.server_address[1]
        self._configure_handler()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port, self.base_url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    @property
    def ac_repair_service_id(self) -> str:
        return _AC_REPAIR_ID
