"""Dashboard-forced session models: saved by account_ui, attached by account_inject, listed from traffic."""

import importlib.util
import json
import runpy
import sqlite3
import time
from pathlib import Path

import pytest
from mitmproxy import http
from mitmproxy.test import tflow

SCRIPTS = Path(__file__).parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("account_inject", SCRIPTS / "account_inject.py")
inject_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inject_module)
ui = runpy.run_path(str(SCRIPTS / "account_ui.py"), run_name="account_ui")
SCHEMA = runpy.run_path(str(SCRIPTS / "account_traffic.py"))["SCHEMA"]

ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"
HEADER = inject_module.SESSION_MODEL_HEADER


def accounts_file(tmp_path, **extra):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"main": "a", "accounts": {"a": 1, "b": 2}, "disabled": ["b"], **extra}))
    return path


def dashboard(path):
    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.accounts_file = path
    board.links = None
    board.state = lambda: json.loads(path.read_text())
    return board


def test_forcing_and_clearing_keeps_every_other_setting_and_session(tmp_path):
    path = accounts_file(tmp_path, session_models={TWO: "gpt-6.1-sol"})
    board = dashboard(path)
    saved = board.update({"session_models": {ONE: " claude-mythos-1[1m] "}})
    assert saved["session_models"] == {TWO: "gpt-6.1-sol", ONE: "claude-mythos-1[1m]"}
    assert saved["disabled"] == ["b"] and list(saved["accounts"]) == ["a", "b"] and saved["main"] == "a"
    saved = board.update({"session_models": {TWO: None}})
    assert saved["session_models"] == {ONE: "claude-mythos-1[1m]"}
    assert "session_models" not in board.update({"session_models": {ONE: None}}), "the last clear drops the key"


@pytest.mark.parametrize("change", [
    {"session_models": [ONE]},
    {"session_models": {"": "gpt-6.1-sol"}},
    {"session_models": {"bad id\r\nx-evil: 1": "gpt-6.1-sol"}},
    {"session_models": {ONE: ""}},
    {"session_models": {ONE: "gpt 6"}},
    {"session_models": {ONE: "gpt\r\nx-evil: 1"}},
    {"session_models": {ONE: 5}},
])
def test_invalid_overrides_are_refused_and_nothing_is_written(tmp_path, change):
    path = accounts_file(tmp_path)
    before = path.read_text()
    with pytest.raises((ValueError, TypeError)):
        dashboard(path).update(change)
    assert path.read_text() == before


def addon(monkeypatch, path):
    monkeypatch.setattr(inject_module, "slot_token", lambda slot: (f"real-{slot}", time.time() * 1000 + 3_600_000))
    inject = inject_module.AccountInject()
    inject.read_accounts(path)
    return inject


def request(path, session=None, authorization="Bearer proxy-account-a", spoof=None, method="POST"):
    flow = tflow.tflow()
    headers = {"Authorization": authorization}
    if session:
        headers[inject_module.SESSION_HEADER] = session
    if spoof:
        headers[HEADER] = spoof
    flow.request = http.Request.make(method, "https://api.anthropic.com" + path, headers=headers)
    return flow


def test_messages_and_counts_carry_only_their_own_sessions_override(monkeypatch, tmp_path):
    inject = addon(monkeypatch, accounts_file(tmp_path, session_models={ONE: "claude-mythos-1"}))
    for path in ("/v1/messages?beta=true", "/v1/messages/count_tokens?beta=true"):
        flow = request(path, ONE)
        inject.requestheaders(flow)
        assert flow.request.headers[HEADER] == "claude-mythos-1", path
        other = request(path, TWO)
        inject.requestheaders(other)
        assert HEADER not in other.request.headers, "another session keeps its own model"
    own_login = request("/v1/messages", ONE, authorization="Bearer own-real-token")
    inject.requestheaders(own_login)
    assert own_login.request.headers[HEADER] == "claude-mythos-1"
    for method, path in (("GET", "/v1/messages"), ("POST", "/api/oauth/usage")):
        flow = request(path, ONE, method=method)
        inject.requestheaders(flow)
        assert HEADER not in flow.request.headers, (method, path)


def test_a_client_supplied_override_never_survives(monkeypatch, tmp_path):
    inject = addon(monkeypatch, accounts_file(tmp_path, session_models={ONE: "claude-mythos-1"}))
    for session, expected in ((TWO, None), (ONE, "claude-mythos-1"), (None, None)):
        flow = request("/v1/messages", session, spoof="gpt-6.1-sol")
        inject.requestheaders(flow)
        assert flow.request.headers.get(HEADER) == expected


def test_an_override_saved_in_the_dashboard_applies_without_a_restart(monkeypatch, tmp_path):
    path = accounts_file(tmp_path)
    inject = addon(monkeypatch, path)
    dashboard(path).update({"session_models": {ONE: "gpt-6.1-sol"}})
    import os
    os.utime(path, (time.time() + 5, time.time() + 5))
    flow = request("/v1/messages", ONE)
    inject.requestheaders(flow)
    assert flow.request.headers[HEADER] == "gpt-6.1-sol"


def test_an_unsafe_override_in_the_file_is_refused(tmp_path):
    with pytest.raises(ValueError):
        inject_module.AccountInject().read_accounts(accounts_file(tmp_path, session_models={ONE: "a\r\nb"}))


def test_sessions_list_recent_inference_by_session_with_overrides(tmp_path):
    db_path = tmp_path / "traffic.db"
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    now = time.time()
    rows = [
        ("1", now - 300, "box", "me", "/home/me/old", ONE, "a/claude-opus-4-7", "claude-opus-4-7", 200),
        ("2", now - 60, "box", "me", "/home/me/proj", ONE, "a/claude-mythos-1", "claude-opus-4-7", 200),
        ("3", now - 30, "laptop", "you", "/w", TWO, None, "gpt-6.1-sol", 429),
        ("4", now - 10, "laptop", "you", "/w", None, None, "gpt-6.1-sol", 200),
        ("5", now - 5, "laptop", "you", "/w", TWO, None, None, 200),
        ("6", now - 30 * 86400, "box", "me", "/w", "33333333-3333-4333-8333-333333333333", None, "x", 200),
    ]
    for fid, started, host, user, cwd, session, served, model, status in rows:
        db.execute("INSERT INTO flows (id, started, client_host, client_user, cwd, session_id, served_by, model, status) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (fid, started, host, user, cwd, session, served, model, status))
    db.commit()
    stale, old = "44444444-4444-4444-8444-444444444444", "33333333-3333-4333-8333-333333333333"
    board = dashboard(accounts_file(tmp_path, traffic_db=str(db_path),
                                    session_models={ONE: "claude-mythos-1", stale: "gpt-6.1-sol", old: "claude-mythos-1"}))
    listed = board.sessions()
    assert (listed["days"], listed["limit"], listed["traffic_db"]) == (7, 50, True)
    first, second, older, third = listed["sessions"]
    assert first["session_id"] == TWO and first["requests"] == 1 and first["status"] == 429 and first["forced_model"] is None
    assert second["session_id"] == ONE and second["requests"] == 2
    assert (second["cwd"], second["requested_model"], second["served_by"], second["forced_model"]) == (
        "/home/me/proj", "claude-opus-4-7", "a/claude-mythos-1", "claude-mythos-1"), "the latest request describes it"
    assert (older["session_id"], older["last_seen"], older["forced_model"]) == (old, now - 30 * 86400, "claude-mythos-1"), \
        "an override outside the recent list keeps its latest activity"
    assert third == {"session_id": stale, "forced_model": "gpt-6.1-sol", "last_seen": None, "remote": None}, "old overrides stay clearable"


def test_sessions_without_a_traffic_database_are_unknown_not_empty(tmp_path):
    listed = dashboard(accounts_file(tmp_path)).sessions()
    assert listed["traffic_db"] is False and listed["sessions"] == []
