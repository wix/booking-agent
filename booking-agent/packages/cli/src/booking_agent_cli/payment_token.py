"""Where UCP checkout gets the shared payment token (SPT) it pays with."""
from __future__ import annotations

from typing import Protocol

from .link_cli import retrieve_spend_request_shared_payment_token


class SharedPaymentTokenSource(Protocol):
    def token_for(self, spend_request_id: str) -> str:
        """Return the SPT for an approved spend request, or raise."""
        ...


class LinkSharedPaymentTokenSource:
    """Reads the SPT from the user's approved Link spend request."""

    def token_for(self, spend_request_id: str) -> str:
        return retrieve_spend_request_shared_payment_token(spend_request_id)


def shared_payment_token_source() -> SharedPaymentTokenSource:
    return LinkSharedPaymentTokenSource()
