"""Saved booking state written by a different booking-agent version."""
from __future__ import annotations

import json

import pytest

from booking_agent_cli import session as sess
from booking_agent_cli.main import run
from booking_agent_cli.support_report import project_session


def _write(path, doc):
    path.write_text(json.dumps(doc))


def _store_doc(**session_extra):
    session = {
        "id": "s1",
        "schema_version": sess.SCHEMA_VERSION,
        "user_request": "haircut",
        "flow_phase": "CONFIRMED",
        **session_extra,
    }
    return {
        "schema_version": sess.STORE_SCHEMA_VERSION,
        "sessions_created": 1,
        "active_session_id": "s1",
        "sessions": [session],
    }


@pytest.fixture
def store_file(isolated_session):
    return sess.session_path()


def test_a_store_from_a_newer_version_is_refused(store_file):
    doc = _store_doc()
    doc["schema_version"] = sess.STORE_SCHEMA_VERSION + 1
    _write(store_file, doc)
    with pytest.raises(sess.SessionStoreTooNew) as exc:
        sess.load_store()
    assert exc.value.found == sess.STORE_SCHEMA_VERSION + 1
    assert json.loads(store_file.read_text()) == doc


def test_a_session_from_a_newer_version_is_refused(store_file):
    doc = _store_doc()
    doc["sessions"][0]["schema_version"] = sess.SCHEMA_VERSION + 1
    _write(store_file, doc)
    with pytest.raises(sess.SessionStoreTooNew):
        sess.load_store()


def test_unknown_keys_survive_a_load_and_save(store_file):
    doc = _store_doc(
        future_flag=True,
        booking={"booking_id": "b1", "credential": {"kind": "future"}},
        payment={"captured_unconfirmed": True, "declined_spend_request_id": "sr_9"},
        business={"name": "Salon", "future_rating": 5},
    )
    doc["future_store_key"] = {"a": 1}
    _write(store_file, doc)

    sess.save_store(sess.load_store())

    saved = json.loads(store_file.read_text())
    assert saved["future_store_key"] == {"a": 1}
    s = saved["sessions"][0]
    assert s["future_flag"] is True
    assert s["booking"]["credential"] == {"kind": "future"}
    assert s["payment"]["declined_spend_request_id"] == "sr_9"
    assert s["payment"]["captured_unconfirmed"] is True
    assert s["business"]["future_rating"] == 5


def test_known_fields_win_over_a_stale_unknown_copy(store_file):
    _write(store_file, _store_doc(future_flag=1))
    store = sess.load_store()
    store.sessions[0].flow_phase = "INIT"
    sess.save_store(store)
    saved = json.loads(store_file.read_text())["sessions"][0]
    assert saved["flow_phase"] == "INIT"
    assert saved["future_flag"] == 1


def test_a_v1_store_still_migrates(store_file):
    _write(store_file, {"schema_version": 1, "id": "old", "user_request": "massage", "flow_phase": "INIT"})
    store = sess.load_store()
    assert [s.user_request for s in store.sessions] == ["massage"]


def test_the_support_report_projection_drops_unknown_keys(store_file):
    _write(store_file, _store_doc(future_secret="tok_live_123", payment={"future_card": "4242"}))
    session = sess.load_store().sessions[0]
    assert "tok_live_123" not in json.dumps(project_session(session), default=str)
    assert "4242" not in json.dumps(project_session(session), default=str)


def test_the_cli_names_the_problem_and_points_at_the_update_check(store_file, monkeypatch, capsys):
    doc = _store_doc()
    doc["schema_version"] = sess.STORE_SCHEMA_VERSION + 1
    _write(store_file, doc)
    monkeypatch.setattr("sys.argv", ["booking-agent", "flow", "status"])
    with pytest.raises(SystemExit) as exc:
        run()
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["error"]["code"] == "session_store_too_new"
    assert out["next_command"] == "booking-agent update check"
    assert json.loads(store_file.read_text()) == doc
