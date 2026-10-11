"""Test helpers for stubbing link-cli subprocess calls."""
from __future__ import annotations

import json
import subprocess
from typing import Optional
from unittest.mock import MagicMock

YUMA_SHIPPING_ADDRESS = {
    "id": "csmrsa_test_default",
    "is_default": True,
    "address": {
        "name": "Ada Lovelace",
        "line_1": "100 Test Street",
        "locality": "Yuma",
        "administrative_area": "AZ",
        "postal_code": "85364",
        "country_code": "US",
    },
}


def _completed(payload) -> MagicMock:
    completed = MagicMock()
    completed.stdout = json.dumps(payload)
    completed.stderr = ""
    completed.returncode = 0
    return completed


def mock_link_cli_user_info(
    monkeypatch, payload: dict, *, shipping_addresses: Optional[list] = None,
) -> None:
    """Stub ``user-info retrieve`` and ``shipping-address list`` (default: no addresses)."""
    user_info = _completed(payload)
    addresses = _completed(list(shipping_addresses or []))
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        parts = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        flat = " ".join(str(p) for p in parts)
        if "user-info" in flat and "retrieve" in flat:
            if kwargs.get("check") and user_info.returncode != 0:
                raise subprocess.CalledProcessError(
                    user_info.returncode, cmd, user_info.stdout, user_info.stderr
                )
            return user_info
        if "shipping-address" in flat and "list" in flat:
            return addresses
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/link-cli")
    monkeypatch.setattr("booking_agent_cli.commands.flow.subprocess.run", fake_run)
    monkeypatch.setattr("booking_agent_cli.commands.flow.shutil.which", lambda _name: "/usr/bin/link-cli")
    monkeypatch.setattr("booking_agent_cli.link_cli.subprocess.run", fake_run)
    monkeypatch.setattr("booking_agent_cli.link_cli.shutil.which", lambda _name: "/usr/bin/link-cli")
