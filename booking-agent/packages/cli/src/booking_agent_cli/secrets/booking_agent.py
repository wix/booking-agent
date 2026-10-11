"""Production secrets mode (``booking-agent flow --secrets-mode booking-agent``).

Production identity is the hosted platform profile document that businesses
fetch on first contact. The identity URI and public registry use canonical
defaults; operator environment variables can override them. Backend, merchant
credentials, and calendar credentials come from the operator environment.

``DEMO_DEFAULTS`` from ``secrets.demo`` are never applied here. A production
install must not silently inherit demo identity or demo endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass

from booking_agent.platform_profile import platform_profile_url


class ProductionSecretsError(RuntimeError):
    """Raised when production secrets mode is misconfigured."""


@dataclass(frozen=True)
class ProductionSecrets:
    """Production configuration resolved for one session."""

    agent_profile_url: str


def load_production_secrets() -> ProductionSecrets:
    """Resolve production config, or raise ``ProductionSecretsError``.

    Fails closed on a non-``https`` identity URI: conformant businesses reject
    those, so a dev or loopback override must not be carried into production.
    """
    profile_url = platform_profile_url()
    if not profile_url.lower().startswith("https://"):
        raise ProductionSecretsError(
            "secrets-mode `booking-agent` needs an https platform profile URL that "
            f"businesses can fetch, but USP_AGENT_PROFILE_URL is {profile_url!r}. "
            "Unset it to use the hosted profile, or point it at an https "
            "staging profile."
        )
    return ProductionSecrets(agent_profile_url=profile_url)
