"""Request bodies sent to businesses use the spec's snake_case names (USP §1.1).

The wire models carry a camelCase alias generator so they can parse registries
that answer in camelCase. Dumping them with ``by_alias=True`` turned that
tolerance into output: ``confirm-payment`` sent ``transactionId`` and
``amountPaid``, which a business reading ``transaction_id`` treats as unknown
members, so the payment it is asked to verify carries no transaction at all.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

import pytest

from booking_agent.discovery import USPBusinessInfo, USPBusinessProfile, USPTransportBinding
from booking_agent.models.wire import PostPaymentReturnRequest, USPPaymentResult
from booking_agent.rest_client import USPBusinessRestClient


class _Sent(Exception):
    pass


def _client(sent: List[Dict[str, Any]]) -> USPBusinessRestClient:
    profile = USPBusinessProfile(
        version="2026-08-20",
        services={
            "dev.usp-protocol.services": [
                USPTransportBinding(
                    version="2026-08-20",
                    transport="rest",
                    endpoint="https://biz.example.com/usp/v1",
                )
            ]
        },
        capabilities={},
        business=USPBusinessInfo(name="Biz", timezone="UTC", currency="USD"),
    )
    client = USPBusinessRestClient(profile, agent_profile_url=None)

    async def capture(url: str, body: Dict[str, Any], **_kwargs: Any) -> dict:
        sent.append(body)
        raise _Sent

    client._post = capture  # type: ignore[method-assign]
    return client


def _member_names(node: Any, *, skip: frozenset = frozenset()) -> List[str]:
    names: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            names.append(key)
            if key not in skip:
                names.extend(_member_names(value, skip=skip))
    elif isinstance(node, list):
        for item in node:
            names.extend(_member_names(item, skip=skip))
    return names


def _assert_snake_case(body: Dict[str, Any], *, data_maps: frozenset = frozenset()) -> None:
    restyled = [n for n in _member_names(body, skip=data_maps) if re.search(r"[A-Z]", n)]
    assert restyled == [], f"camelCase member names on the wire: {restyled}"


@pytest.mark.asyncio
async def test_confirm_payment_sends_snake_case_payment_result():
    sent: List[Dict[str, Any]] = []
    client = _client(sent)
    with pytest.raises(_Sent):
        await client.confirm_payment(
            "bkg_1",
            USPPaymentResult(
                status="succeeded",
                provider="stripe",
                transaction_id="pi_123",
                amount_paid=12000,
                currency="USD",
                order_reference="ord_9",
            ),
        )

    assert sent[0] == {
        "payment_result": {
            "status": "succeeded",
            "provider": "stripe",
            "transaction_id": "pi_123",
            "amount_paid": 12000,
            "currency": "USD",
            "order_reference": "ord_9",
        }
    }
    _assert_snake_case(sent[0])


@pytest.mark.asyncio
async def test_create_booking_sends_snake_case_names():
    sent: List[Dict[str, Any]] = []
    client = _client(sent)
    with pytest.raises(_Sent):
        await client.create_booking(
            "svc_1",
            "slot_1",
            {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"},
            hold_id="hold_1",
        )

    _assert_snake_case(sent[0])


def test_post_payment_return_request_dumps_snake_case_names():
    dumped = PostPaymentReturnRequest(
        url="https://agent.example/return",
        params={"sessionId": "opaque"},
    ).model_dump(exclude_none=True)
    assert dumped == {"url": "https://agent.example/return", "params": {"sessionId": "opaque"}}
    # ``params`` is a map of opaque values the business echoes back, not
    # USP-defined member names, so its keys are the caller's to choose.
    _assert_snake_case(dumped, data_maps=frozenset({"params"}))
