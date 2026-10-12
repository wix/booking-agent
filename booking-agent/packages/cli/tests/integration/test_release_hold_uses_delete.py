"""``release_hold`` uses DELETE per the USP OpenAPI (not POST /release).

Rewritten onto the ``httpx.MockTransport`` idiom.  The previous version patched
``httpx.AsyncClient`` with a fake exposing only ``.delete``, which meant it
exercised a bespoke code path rather than the shared request path -- so it kept
passing while ``release_hold`` carried no authorization and no 401 handling.
``usp_availability_release`` is privileged_scoped, so the request it makes is
now part of what this test pins down.
"""
import httpx
import pytest

from booking_agent.authorization import BookingScopedCredential
from booking_agent.discovery import USPBusinessInfo, USPBusinessProfile, USPTransportBinding
from booking_agent.pop import PopKey
from booking_agent.rest_client import USPBusinessRestClient

ENDPOINT = "https://biz.example.com/usp/v1"


def _profile() -> USPBusinessProfile:
    return USPBusinessProfile(
        version="2026-08-14",
        services={
            "dev.usp-protocol.services": [
                USPTransportBinding(
                    version="2026-08-14",
                    transport="rest",
                    endpoint=ENDPOINT,
                )
            ]
        },
        capabilities={},
        checkout_systems=["redirect"],
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )


def _capture(monkeypatch, requests: list[httpx.Request]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method != "DELETE":
            raise AssertionError("release_hold MUST use DELETE per the USP spec")
        return httpx.Response(200, json={"hold_id": "hold_abc", "status": "released"})

    transport = httpx.MockTransport(handler)

    def fake_traced(**kwargs):
        merged = dict(kwargs)
        merged.setdefault("follow_redirects", True)
        return httpx.AsyncClient(transport=transport, **merged)

    monkeypatch.setattr("booking_agent.rest_client.traced_async_client", fake_traced)


@pytest.mark.asyncio
async def test_release_hold_calls_delete(monkeypatch):
    requests: list[httpx.Request] = []
    _capture(monkeypatch, requests)

    client = USPBusinessRestClient(_profile(), agent_profile_url=None)
    await client.release_hold("hold_abc")

    assert requests[0].method == "DELETE"
    assert str(requests[0].url) == f"{ENDPOINT}/availability/holds/hold_abc"


@pytest.mark.asyncio
async def test_release_hold_goes_through_the_authorized_request_path(monkeypatch):
    """The point of routing it through ``_request``: it can now be authorized."""
    requests: list[httpx.Request] = []
    _capture(monkeypatch, requests)

    key = PopKey.generate()
    credential = BookingScopedCredential.model_validate({
        "value": "bsc_release_hold_credential_value",
        "expires_at": "2099-01-01T00:00:00Z",
        "cnf": {"jkt": key.jkt},
    })
    client = USPBusinessRestClient(
        _profile(), agent_profile_url=None, pop_key=key, authenticate=True,
    )
    await client.release_hold("hold_abc", credential=credential)

    request = requests[0]
    assert request.headers["Authorization"] == f"DPoP {credential.value}"
    assert request.headers["DPoP"], "a proof must accompany the credential"
    assert request.headers["Idempotency-Key"]
