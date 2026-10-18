"""Every failure seen in the recorded hosted runs maps to a test that still exists.

Two recorded runs of the same booking (an AC repair, paid from the Link wallet, added to the
calendar) succeeded but stalled: sign-in and calendar access lost after a restart and asked for
again one at a time, silence after "yes" and after the approval link, and a calendar event the
assistant could not show. These were fixed before in the instructions alone and came back. Each
entry below names the test that now holds the fix in code; removing one fails the build.
"""
from pathlib import Path

_TESTS = Path(__file__).resolve().parent

REGRESSIONS = {
    "state split across two persistent roots": [
        ("test_runtime_prepare.py", "test_a_root_already_named_booking_agent_is_not_nested_again"),
        ("test_runtime_prepare.py", "test_moving_to_a_different_root_brings_the_saved_state_along"),
        ("test_runtime_prepare.py", "test_moving_to_a_root_that_has_its_own_state_leaves_the_old_one_untouched"),
        ("test_runtime_prepare.py", "test_a_state_folder_nested_by_the_old_layout_is_lifted_back_up"),
        ("test_runtime_prepare.py", "test_doctor_reports_where_state_lives_and_what_is_orphaned"),
    ],
    "sign-in approved but the poll never reported, so the user is asked again": [
        ("test_link_auth.py", "test_buyer_resolve_trusts_link_when_the_sign_in_poll_never_reported"),
    ],
    "Link and calendar reconnected one after another, after the user said yes": [
        ("test_link_auth.py", "test_connections_ensure_reports_link_and_calendar_in_one_message"),
        ("test_link_auth.py", "test_buyer_resolve_catches_a_lost_calendar_before_the_confirmation"),
    ],
    "agent had nothing to say after the approval; the first object sent it off to poll again": [
        ("test_flow_gates.py", "test_pay_poll_emits_one_terminal_booked_object"),
        ("test_flow_gates.py", "test_status_answers_after_the_booking_is_done_with_the_same_message"),
    ],
    "calendar event existed but was reported as unconfirmed, or its failure hid the booking": [
        ("test_flow_gates.py", "test_status_answers_after_the_booking_is_done_with_the_same_message"),
        ("test_flow_gates.py", "test_a_calendar_failure_after_payment_still_reports_the_booking"),
    ],
    "search results lost, forcing a second search": [
        ("test_link_auth.py", "test_status_hands_back_saved_search_results_instead_of_a_new_search"),
    ],
    "slot picked without the confirmation flag from the default command": [
        ("test_link_auth.py", "test_the_default_next_step_after_listing_slots_carries_the_confirmation_flag"),
    ],
    "an earlier session with a payment in progress was abandoned": [
        ("test_link_auth.py", "test_init_warns_before_abandoning_a_session_with_a_payment_in_progress"),
    ],
    "the same booking was started twice; the question was left to the skill to remember": [
        ("test_link_auth.py", "test_init_asks_whether_to_resume_a_similar_unfinished_request"),
    ],
    "restart plus a differently named persistent folder mid-booking (offline replay)": [
        ("../e2e/test_ucp_skill_flow_e2e.py", "test_a_restart_and_a_changed_persistent_root_mid_booking_lose_nothing"),
    ],
    "approval link shown twice": [
        ("test_flow_gates.py", "test_flow_status_payment_pending_does_not_resurface_approval_url"),
    ],
}


def test_every_recorded_failure_still_has_its_test():
    missing = []
    for failure, tests in REGRESSIONS.items():
        for filename, name in tests:
            source = (_TESTS / filename).read_text(encoding="utf-8")
            if f"def {name}(" not in source:
                missing.append(f"{failure}: {filename}::{name}")
    assert missing == []
