"""End-to-end regression: Asia/Jerusalem business, America/Phoenix buyer, free booking.

One hermetic flow from discovery through cancellation. The business zone comes
from the profile, the buyer zone from the Link default shipping address (Yuma,
AZ), so every customer-facing time must be shown in both zones and the free
route must never mention charging the wallet.
"""
from support.fixtures.services import set_zero_price_free_service
from support.mock.link_cli import YUMA_SHIPPING_ADDRESS, mock_link_cli_user_info
from integration.test_flow_gates import (
    _BUYER,
    _SERVICE_ID,
    _YUMA,
    _business_use,
    _geocoder,
    _init,
    _invoke,
    _select_first_and_resolve,
    _stdout_json,
)

_BUSINESS_TZ = "Asia/Jerusalem"
_BUYER_TZ = "America/Phoenix"


def _ok(result) -> dict:
    assert result.exit_code == 0, result.stdout + result.stderr
    return _stdout_json(result)


def _set_business_timezone(monkeypatch, timezone_name):
    """Publish ``timezone_name`` from both the UCP profile resolver and the backend."""
    import booking_agent.ucp_profile as ucp_profile
    import booking_agent_cli.commands.flow as flow

    def zoned(profile):
        business = profile.business.model_copy(update={"timezone": timezone_name})
        return profile.model_copy(update={"business": business})

    original_fetch = ucp_profile.fetch_and_resolve_business_profile

    async def zoned_fetch(url):
        profile, uctx = await original_fetch(url)
        return zoned(profile), uctx

    monkeypatch.setattr(ucp_profile, "fetch_and_resolve_business_profile", zoned_fetch)

    backend = flow.resolve_backend()
    original_resolve = backend.resolve_profile

    async def zoned_resolve(url):
        return zoned(await original_resolve(url))

    monkeypatch.setattr(backend, "resolve_profile", zoned_resolve)


def test_jerusalem_business_phoenix_buyer_free_booking_end_to_end(
    runner, isolated_session, monkeypatch,
):
    from booking_agent_cli.session import is_actionable, must_load

    _set_business_timezone(monkeypatch, _BUSINESS_TZ)
    set_zero_price_free_service(monkeypatch, _SERVICE_ID)
    mock_link_cli_user_info(monkeypatch, _BUYER, shipping_addresses=[YUMA_SHIPPING_ADDRESS])
    _geocoder(monkeypatch, _YUMA)

    _init(runner)
    use = _ok(_business_use(runner))
    assert use["data"]["business_timezone"] == _BUSINESS_TZ
    assert use["data"]["payment_route"] == "free"

    _ok(_invoke(runner, "flow", "calendar", "skip"))
    avail = _ok(_invoke(runner, "flow", "availability", "--service-id", _SERVICE_ID))
    assert avail["data"]["business_timezone"] == _BUSINESS_TZ
    assert avail["data"]["buyer_timezone"] == _BUYER_TZ

    # Slot list: one notice, every slot in both zones.
    slots = _ok(_invoke(runner, "flow", "slots", "list"))
    data = slots["data"]
    assert data["business_timezone"] == _BUSINESS_TZ
    assert data["buyer_timezone"] == _BUYER_TZ
    notice = data["timezone_notice"]
    assert notice.startswith("I'm detecting a timezone difference")
    assert "Jerusalem time (Asia/Jerusalem)" in notice
    assert "Phoenix time (America/Phoenix)" in notice
    assert data["slots"]
    for slot in data["slots"]:
        assert slot["business_start_display"].endswith(f"({_BUSINESS_TZ})")
        assert slot["buyer_start_display"].endswith(f"({_BUYER_TZ})")
    first = data["slots"][0]
    # Mock slots start at 14:00 UTC: 16:00 or 17:00 in Jerusalem, 07:00 in Phoenix.
    assert any(h in first["business_start_display"] for h in ("4:00 PM IST", "5:00 PM IDT"))
    assert "7:00 AM MST" in first["buyer_start_display"]
    session = must_load()
    assert session.buyer_timezone == _BUYER_TZ
    assert session.buyer_timezone_source == "link_shipping_address"
    assert session.slots_presented_dual_zone is True

    # Single confirmation: both times, free copy, no wallet or charge wording.
    recap = _select_first_and_resolve(runner, slots)
    rdata = recap["data"]
    selected = rdata["selected_slot"]
    assert selected["business_display"].startswith(first["business_start_display"] + " to ")
    assert selected["business_display"].endswith(f"({_BUSINESS_TZ})")
    assert selected["buyer_display"].startswith(first["buyer_start_display"] + " to ")
    assert selected["buyer_display"].endswith(f"({_BUYER_TZ})")
    assert "timezone_notice" not in rdata
    assert rdata["payment_route"] == "free"
    assert rdata["price_display"] == "$0.00"
    assert rdata["payment_summary"]["route"] == "free"
    assert rdata["payment_summary"]["charge_now_display"] is None
    prompt = recap["prompt"]
    assert "buyer_display in parentheses" in prompt
    assert "this service is free" in prompt
    assert "wallet" not in prompt.lower()
    assert "charge" not in prompt.lower()
    assert recap["next_command"] == "booking-agent flow book"
    assert must_load().timezone_difference_shown is True

    # Book: no payment request, poll next, narration says nothing was charged.
    booked = _ok(_invoke(runner, "flow", "book", "--service-id", _SERVICE_ID))
    assert booked["next_command"] == "booking-agent flow book poll --wait"
    bdata = booked["data"]
    assert not bdata.get("payment_request")
    assert not bdata.get("spend_request_id")
    assert must_load().payment.spend_request_id is None
    narration = bdata["booking_narration"]
    assert "nothing was charged" in narration.lower()
    assert "free" in narration
    assert "wallet" not in narration.lower()
    assert "wallet" not in booked["prompt"].lower()

    polled = _ok(_invoke(runner, "flow", "book", "poll", "--wait", "--timeout", "10"))
    assert polled["phase"] == "CONFIRMED"
    assert must_load().flow_phase == "CONFIRMED"

    _ok(_invoke(runner, "flow", "validate-skill-compliance"))

    canceled = _ok(_invoke(runner, "flow", "book", "cancel", "--reason", "changed plans"))
    assert canceled["data"]["status"] == "canceled"

    session = must_load()
    assert session.booking.status == "canceled"
    assert is_actionable(session) is False

    rows = _ok(_invoke(runner, "flow", "sessions", "list"))["data"]["sessions"]
    row = next(r for r in rows if r["id"] == session.id)
    assert row["booking_status"] == "canceled"
    assert row["actionable"] is False

    preview = _ok(_invoke(runner, "flow", "sessions", "cancel-open"))["data"]
    assert preview["mode"] == "preview"
    assert preview["bookings"] == []
