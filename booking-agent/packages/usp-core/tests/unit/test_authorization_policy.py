"""``AuthorizationPolicy`` parsing, mechanism selection, and posture (USP Section 10.1.6)."""
from __future__ import annotations

import pytest

from booking_agent.authorization import (
    AuthorizationPolicy,
    AuthorizationRefused,
    ScopedCredentialPosture,
    evaluate,
    parse_policy,
    resolve_posture,
)
from booking_agent.discovery import USPProfileResolver
from booking_agent.ucp_profile import UcpNativeContext, parse_ucp_native_context_from_document

REQUIRED = ScopedCredentialPosture.REQUIRED
PREFERRED = ScopedCredentialPosture.PREFERRED


def _policy(**overrides) -> AuthorizationPolicy:
    base = {
        "privileged_operations_require_authentication": True,
        "accepted_mechanisms": ["platform_key_pop"],
    }
    base.update(overrides)
    return AuthorizationPolicy.model_validate(base)


# -- Parsing ---------------------------------------------------------------

def test_standalone_policy_is_parsed_from_the_profile():
    profile = USPProfileResolver._parse_profile({
        "usp": {
            "version": "2026-08-14",
            "services": {},
            "capabilities": {},
            "business": {"name": "Biz"},
            "authorization": {
                "privileged_operations_require_authentication": True,
                "accepted_mechanisms": ["platform_key_pop", "oauth2_bearer"],
            },
        }
    })
    assert profile.authorization is not None
    assert profile.authorization.select_mechanism() == "platform_key_pop"


def test_policy_is_open_to_extension():
    """additionalProperties: true -- accepted algorithms ride on this object."""
    policy = _policy(accepted_algorithms=["EdDSA", "ES256"])
    assert policy.model_extra["accepted_algorithms"] == ["EdDSA", "ES256"]


def test_policy_missing_a_required_member_is_treated_as_absent():
    assert parse_policy({"accepted_mechanisms": ["platform_key_pop"]}) is None
    assert parse_policy({"privileged_operations_require_authentication": True}) is None
    assert parse_policy("not-an-object") is None
    assert parse_policy(None) is None


def _ucp_document(binding: dict) -> dict:
    return {
        "ucp": {
            "version": "2026-01-11",
            "capabilities": {
                "dev.ucp.shopping.checkout": [{"version": "2026-01-11"}],
                "dev.usp-protocol.services.paid_bookings": [{
                    "version": "2026-08-14",
                    "extends": "dev.ucp.shopping.checkout",
                }],
            },
            "services": {
                "dev.ucp.shopping": [
                    {"version": "2026-01-11", "transport": "rest",
                     "endpoint": "https://b.example/ucp"}
                ],
                "dev.usp-protocol.services": [binding],
            },
        }
    }


def test_ucp_native_policy_is_read_from_config_authorization():
    ctx = parse_ucp_native_context_from_document(_ucp_document({
        "version": "2026-08-14",
        "transport": "rest",
        "endpoint": "https://b.example/usp",
        "config": {
            "authorization": {
                "privileged_operations_require_authentication": True,
                "accepted_mechanisms": ["platform_key_pop"],
            }
        },
    }))
    policy = ctx.usp_authorization()
    assert policy is not None
    assert policy.select_mechanism() == "platform_key_pop"
    assert not ctx.has_misplaced_binding_authorization()


def test_sibling_authorization_on_a_binding_is_rejected_not_read():
    """``schemas/usp.json`` gives it a never-satisfiable schema, so it must be
    rejected rather than silently accepted -- and we must NOT fall back to
    reading it, or a business would believe it published a policy that no
    conforming consumer reads."""
    ctx = parse_ucp_native_context_from_document(_ucp_document({
        "version": "2026-08-14",
        "transport": "rest",
        "endpoint": "https://b.example/usp",
        "authorization": {
            "privileged_operations_require_authentication": True,
            "accepted_mechanisms": ["platform_key_pop"],
        },
    }))
    assert ctx.has_misplaced_binding_authorization()
    assert ctx.usp_authorization() is None  # treated as "no policy published"


# -- Mechanism selection ---------------------------------------------------

def test_unrecognized_mechanisms_are_ignored_never_accepted():
    policy = _policy(accepted_mechanisms=["quantum_handshake", "platform_key_pop"])
    assert policy.select_mechanism() == "platform_key_pop"

    unknown_only = _policy(accepted_mechanisms=["quantum_handshake"])
    assert unknown_only.select_mechanism() is None


# -- Fail-closed behaviour -------------------------------------------------

def test_required_posture_fails_closed_when_no_policy_is_published():
    with pytest.raises(AuthorizationRefused) as caught:
        evaluate(None, posture=REQUIRED, exposes_privileged_capabilities=True)
    assert caught.value.code == "authorization_policy_absent"


def test_required_posture_refuses_a_deployment_declaring_itself_non_conformant():
    """`false` means out of conformance: refuse to transact, not warn and continue."""
    policy = _policy(privileged_operations_require_authentication=False)
    with pytest.raises(AuthorizationRefused) as caught:
        evaluate(policy, posture=REQUIRED)
    assert caught.value.code == "privileged_operations_unauthenticated"


def test_required_posture_fails_closed_with_no_mutually_supported_mechanism():
    policy = _policy(accepted_mechanisms=["mtls"])
    with pytest.raises(AuthorizationRefused) as caught:
        evaluate(policy, posture=REQUIRED)
    assert caught.value.code == "no_supported_mechanism"
    # Never falls back to anonymous.


def test_browse_only_business_needs_no_policy():
    decision = evaluate(None, posture=REQUIRED, exposes_privileged_capabilities=False)
    assert decision.authenticate is False


def test_preferred_posture_warns_and_proceeds():
    """The escape hatch for transacting against a not-yet-conformant business."""
    decision = evaluate(None, posture=PREFERRED, exposes_privileged_capabilities=True)
    assert decision.authenticate is False
    assert decision.warning
    assert decision.refused is False


def test_policy_present_and_supported_selects_pop():
    decision = evaluate(_policy(), posture=REQUIRED)
    assert decision.authenticate is True
    assert decision.uses_pop


# -- Posture resolution ----------------------------------------------------

def test_the_default_posture_is_the_conformant_one(monkeypatch):
    """USP Section 10.1.6 requires privileged operations to be authenticated and
    requires failing closed, so anything laxer would ship a known deviation."""
    monkeypatch.delenv("BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL", raising=False)
    assert resolve_posture() is REQUIRED


def test_posture_precedence_is_explicit_then_env_then_default(monkeypatch):
    monkeypatch.setenv("BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL", "preferred")
    assert resolve_posture() is PREFERRED
    assert resolve_posture("required") is REQUIRED
    assert resolve_posture("disabled") is REQUIRED

    monkeypatch.setenv("BOOKING_AGENT_USP_AUTH_SCOPED_CREDENTIAL", "nonsense")
    assert resolve_posture() is REQUIRED


# -- Deferred refusal: browse yes, book no ---------------------------------

def test_deferred_refusal_is_carried_not_raised():
    decision = evaluate(
        None, posture=REQUIRED, exposes_privileged_capabilities=True, defer=True,
    )
    assert decision.refused
    assert decision.refusal_code == "authorization_policy_absent"
    with pytest.raises(AuthorizationRefused):
        decision.raise_if_refused()


def test_a_conformant_business_carries_no_refusal():
    decision = evaluate(_policy(), posture=REQUIRED, defer=True)
    assert decision.refused is False
    decision.raise_if_refused()  # must not raise
