"""Remote Control links: a proxy session is matched to HQ's session by the reply id both carry."""

import json
import runpy
import sqlite3
import threading
import time
import zlib
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "scripts"
links_module = runpy.run_path(str(SCRIPTS / "account_remote_links.py"), run_name="account_remote_links")
ui = runpy.run_path(str(SCRIPTS / "account_ui.py"), run_name="account_ui")
SCHEMA = runpy.run_path(str(SCRIPTS / "account_traffic.py"))["SCHEMA"]
RemoteLinks = links_module["RemoteLinks"]

ONE = "11111111-1111-4111-8111-111111111111"
TWO = "22222222-2222-4222-8222-222222222222"
RC = "https://api.anthropic.com/v1/code/sessions/"


def database():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    return db


def flow(db, started, url, session_id=None, model=None, request=None, response=None):
    db.execute("INSERT INTO flows (id, started, url, session_id, model, method, request_body, response_body) "
               "VALUES (?, ?, ?, ?, ?, 'POST', ?, ?)",
               (f"f{started}-{url[-12:]}", started, url, session_id, model,
                zlib.compress(json.dumps(request).encode()) if request else None,
                zlib.compress(response.encode()) if response else None))


def reply(message_id):
    return f'event: message_start\ndata: {{"type":"message_start","message":{{"id":"{message_id}","role":"assistant"}}}}\n'


def relayed(message_id):
    return {"events": [{"payload": {"type": "assistant", "message": {"id": message_id, "content": []}}}]}


def hub(*rows):
    return lambda url, timeout=5: [{"id": f"hq-{cse}", "cse": cse, "claude_url": claude} for cse, claude in rows]


def test_each_remote_control_session_links_to_the_session_whose_reply_it_relayed(monkeypatch):
    now = time.time()
    db = database()
    # Two sessions answer within seconds of each other on the same machine; only the reply id tells them apart.
    flow(db, now - 100, "https://api.anthropic.com/v1/messages", ONE, "claude-opus-5-5", response=reply("msg_one"))
    flow(db, now - 99, "https://api.anthropic.com/v1/messages", TWO, "claude-opus-5-5", response=reply("msg_two"))
    flow(db, now - 98, RC + "cse_b/worker/events", request=relayed("msg_two"))
    flow(db, now - 97, RC + "cse_a/worker/events", request=relayed("msg_one"))
    flow(db, now - 96, RC + "cse_a/worker/heartbeat")
    monkeypatch.setitem(RemoteLinks.__call__.__globals__, "hub_sessions",
                        hub(("cse_a", "https://claude.ai/code/cshk_a"), ("cse_b", None), ("cse_unseen", None)))
    links = RemoteLinks("http://hq.test", "https://hq.example.com/")
    found = links(db, 7)
    assert found == {ONE: {"cchq": "https://hq.example.com/#session=hq-cse_a", "claude": "https://claude.ai/code/cshk_a"},
                     TWO: {"cchq": "https://hq.example.com/#session=hq-cse_b", "claude": None}}
    assert set(links.misses) == {"cse_unseen"}, "an unmatched session waits before being searched again"


def test_a_resumed_session_links_to_its_newest_remote_control_session(monkeypatch):
    now = time.time()
    db = database()
    flow(db, now - 900, "https://api.anthropic.com/v1/messages", ONE, "claude-opus-5-5", response=reply("msg_old"))
    flow(db, now - 899, RC + "cse_old/worker/events", request=relayed("msg_old"))
    flow(db, now - 50, "https://api.anthropic.com/v1/messages", ONE, "gpt-6.1-sol", response=reply("msg_new"))
    flow(db, now - 49, RC + "cse_new/worker/events", request=relayed("msg_new"))
    monkeypatch.setitem(RemoteLinks.__call__.__globals__, "hub_sessions", hub(("cse_old", "u-old"), ("cse_new", "u-new")))
    assert RemoteLinks("http://hq.test", "https://hq")(db, 7)[ONE]["claude"] == "u-new"


def test_hq_failure_keeps_the_sessions_and_says_why(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"main": "a", "accounts": {"a": 1}}))
    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.accounts_file, board.links_lock, board.links_at, board.links_result = path, threading.Lock(), 0, ({}, None)

    def down(db, days):
        raise OSError("connection refused, token=secret")
    board.links = down
    assert board.remote_links(database()) == ({}, "cchq unreachable: OSError"), "no exception text reaches the page"


def test_hub_native_route_survives_canonical_uuid_binding(monkeypatch):
    import io
    rows = [
        {"id": "opaque-bound", "transport": "native", "session": "cse_bound", "claude_session_id": ONE,
         "upstream": {"session_url": "https://claude.ai/code/bound"}},
        {"id": "opaque-legacy", "transport": "native", "claude_session_id": "cse_legacy"},
        {"id": "not-native", "transport": "tmux", "session": "cse_other", "claude_session_id": TWO},
        {"id": "invalid-route", "transport": "native", "session": "other", "claude_session_id": TWO},
    ]
    urlopen = links_module["hub_sessions"].__globals__["urllib"].request
    monkeypatch.setattr(urlopen, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps({"sessions": rows}).encode()))
    found = links_module["hub_sessions"]("http://hq.test")
    assert found == [
        {"id": "opaque-bound", "cse": "cse_bound", "claude_url": "https://claude.ai/code/bound", "claude_session_id": ONE},
        {"id": "opaque-legacy", "cse": "cse_legacy", "claude_url": None, "claude_session_id": None},
    ]


def test_canonical_binding_links_without_a_recorded_reply_and_updates_cached_owner(monkeypatch):
    current = ONE
    def bound_hub(*args, **kwargs):
        return [{"id": "opaque", "cse": "cse_bound", "claude_url": None, "claude_session_id": current}]
    scope = RemoteLinks.__call__.__globals__
    monkeypatch.setitem(scope, "hub_sessions", bound_hub)
    monkeypatch.setitem(scope, "owner", lambda *args: (_ for _ in ()).throw(AssertionError("canonical identity must not infer from replies")))
    links = RemoteLinks("http://hq.test", "https://hq")
    links.misses["cse_bound"] = time.time()
    links.owners["cse_bound"] = "stale-cached-owner"
    db = database()
    assert links(db, 7) == {ONE: {"cchq": "https://hq/#session=opaque", "claude": None}}
    assert links.misses == {}
    current = TWO
    assert links(db, 7) == {TWO: {"cchq": "https://hq/#session=opaque", "claude": None}}
    assert links.owners == {"cse_bound": TWO}
