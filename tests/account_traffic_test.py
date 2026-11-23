"""The --serve proxy's traffic recorder: who sent what, and never a credential."""

import importlib.util
import json
import sqlite3
import zlib
from pathlib import Path

from mitmproxy import http, websocket
from mitmproxy.test import tflow

spec = importlib.util.spec_from_file_location(
    "account_traffic", Path(__file__).parents[1] / "scripts/account_traffic.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SYSTEM = [{"type": "text", "text": "You are Claude Code."},
          {"type": "text", "text": "<env>\nPrimary working directory: /work/repo\nPlatform: linux\n</env>"}]


def recorder(tmp_path):
    return module.TrafficDB(lambda: str(tmp_path / "traffic.sqlite3"))


def rows(tmp_path, table="flows"):
    db = sqlite3.connect(tmp_path / "traffic.sqlite3")
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def inference_flow():
    flow = tflow.tflow(resp=True)
    flow.request = http.Request.make(
        "POST",
        "https://api.anthropic.com/v1/messages?beta=true",
        json.dumps({"model": "claude-opus-5-5", "system": SYSTEM,
                    "messages": [{"role": "user", "content": "hi"}]}),
        {
            "authorization": "Bearer proxy-account-user@example.com+rc=other",
            "x-claude-code-session-id": "s-1",
            "x-claude-client-host": "laptop",
            "x-claude-client-user": "dev",
            "x-claude-client-cwd": "/home/dev/%D9%85%D8%B4%D8%B1%D9%88%D8%B9",
        },
    )
    return flow


def test_inference_is_recorded_with_its_sender_and_no_credential(tmp_path):
    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    assert "x-claude-client-host" not in flow.request.headers  # never reaches Anthropic
    flow.request.headers["authorization"] = "Bearer sk-real-secret"  # AccountInject's swap
    flow.metadata["api_key"] = "laptop"
    flow.response = http.Response.make(200, b'{"ok": true}', {"x-claude-codex-served-by": "user@example.com/claude-opus-5-5"})
    traffic.response(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert row["account"] == "proxy-account-user@example.com+rc=other"
    assert (row["client_host"], row["client_user"]) == ("laptop", "dev")
    assert row["launch_cwd"] == "/home/dev/مشروع"
    assert row["cwd"] == "/work/repo"
    assert row["session_id"] == "s-1"
    assert row["model"] == "claude-opus-5-5"
    assert row["served_by"] == "user@example.com/claude-opus-5-5"
    assert row["url"] == "https://api.anthropic.com/v1/messages?beta=true"
    assert row["status"] == 200
    assert row["api_key"] == "laptop"
    assert "sk-real-secret" not in json.dumps(row, default=str)
    assert json.loads(zlib.decompress(row["response_body"])) == {"ok": True}


def test_api_key_name_survives_an_upstream_connection_error(tmp_path):
    from mitmproxy.flow import Error

    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.metadata["api_key"] = "laptop"
    flow.response = None
    flow.error = Error("upstream disconnected")
    traffic.error(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert row["api_key"] == "laptop"
    assert row["error"] == "upstream disconnected"
    assert row["status"] is None


def test_working_directory_from_a_system_role_message():
    request = {"system": [{"type": "text", "text": "You are Claude Code."}], "messages": [
        {"role": "user", "content": "look in /elsewhere"},
        {"role": "system", "content": [{"type": "text", "text": " - Primary working directory: /work/other\n - Platform: linux"}]},
    ]}
    assert module.working_directory(request) == "/work/other"


def test_streamed_replies_are_captured_whole(tmp_path):
    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(200, b"", {"content-type": "text/event-stream"})
    flow.response.stream = True
    traffic.responseheaders(flow)
    assert flow.response.stream(b"event: a\n") == b"event: a\n"
    flow.response.stream(b"event: b\n")
    traffic.response(flow)
    traffic.flush()
    assert zlib.decompress(rows(tmp_path)[0]["response_body"]) == b"event: a\nevent: b\n"


def test_credential_fields_are_redacted_but_token_counts_are_kept(tmp_path):
    traffic = recorder(tmp_path)
    flow = tflow.tflow(resp=True)
    flow.request = http.Request.make("POST", "https://api.anthropic.com/v1/oauth/token", b'{"grant_type": "x"}')
    traffic.requestheaders(flow)
    flow.response = http.Response.make(
        200, json.dumps({"access_token": "a-secret", "refresh_token": "r-secret", "usage": {"output_tokens": 5}})
    )
    traffic.response(flow)
    traffic.flush()
    body = json.loads(zlib.decompress(rows(tmp_path)[0]["response_body"]))
    assert body == {"access_token": "[redacted]", "refresh_token": "[redacted]", "usage": {"output_tokens": 5}}


def test_websocket_subprotocol_tokens_are_redacted_but_the_protocol_name_is_kept(tmp_path):
    traffic = recorder(tmp_path)
    flow = tflow.tflow(resp=True)
    flow.request = http.Request.make(
        "GET",
        "https://api.anthropic.com/api/frame/sync?slug=toy",
        b"",
        {"upgrade": "websocket", "sec-websocket-protocol": "frame-sync.v1,toy-watch-secret"},
    )
    traffic.requestheaders(flow)
    flow.response = http.Response.make(101, b"", {"sec-websocket-protocol": "frame-sync.v1"})
    traffic.response(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert "toy-watch-secret" not in row["request_headers"]
    assert ["sec-websocket-protocol", "frame-sync.v1,[redacted]"] in json.loads(row["request_headers"])
    assert ["sec-websocket-protocol", "frame-sync.v1"] in json.loads(row["response_headers"])


def test_a_lone_watch_token_subprotocol_is_redacted(tmp_path):
    traffic = recorder(tmp_path)
    flow = tflow.tflow(resp=True)
    flow.request = http.Request.make(
        "GET", "https://api.anthropic.com/api/frame/sync?slug=toy", b"", {"sec-websocket-protocol": "toy-watch-secret"}
    )
    traffic.requestheaders(flow)
    traffic.response(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert "toy-watch-secret" not in row["request_headers"]


def test_artifact_watch_tokens_in_boot_replies_and_refresh_frames_are_redacted(tmp_path):
    traffic = recorder(tmp_path)
    flow = tflow.tflow()
    flow.websocket = websocket.WebSocketData()
    frame = {"type": "ctrl:token_refresh", "cap": "toy-cap-secret", "exp": 1}
    flow.websocket.messages.append(websocket.WebSocketMessage(1, False, json.dumps(frame).encode()))
    traffic.websocket_message(flow)
    traffic.flush()
    [row] = rows(tmp_path, "websocket_messages")
    assert json.loads(zlib.decompress(row["body"])) == {"type": "ctrl:token_refresh", "cap": "[redacted]", "exp": 1}
    boot = {"syncToken": "toy-sync-secret", "assetToken": "toy-asset-secret", "slug": "toy"}
    assert module.redact(boot) == {"syncToken": "[redacted]", "assetToken": "[redacted]", "slug": "toy"}


def test_websocket_frames_are_recorded_without_tokens(tmp_path):
    traffic = recorder(tmp_path)
    flow = tflow.tflow()
    flow.websocket = websocket.WebSocketData()
    frame = {"type": "connect", "oauth_token": "proxy-account-user@example.com"}
    flow.websocket.messages.append(websocket.WebSocketMessage(1, True, json.dumps(frame).encode()))
    traffic.websocket_message(flow)
    traffic.flush()
    [row] = rows(tmp_path, "websocket_messages")
    assert json.loads(zlib.decompress(row["body"])) == {"type": "connect", "oauth_token": "[redacted]"}


def test_nothing_is_recorded_without_a_database(tmp_path):
    traffic = module.TrafficDB(lambda: None)
    flow = inference_flow()
    traffic.requestheaders(flow)
    traffic.response(flow)
    traffic.flush()
    assert "x-claude-client-host" in flow.request.headers
    assert not (tmp_path / "traffic.sqlite3").exists()


def test_repeated_conversation_parts_are_stored_once_and_restored(tmp_path):
    import runpy

    traffic = recorder(tmp_path)
    for _ in range(2):
        flow = inference_flow()
        traffic.requestheaders(flow)
        flow.response = http.Response.make(200, b"{}")
        traffic.response(flow)
        traffic.flush()
    db = sqlite3.connect(tmp_path / "traffic.sqlite3")
    assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2  # system + one message
    reader = runpy.run_path(str(Path(__file__).parents[1] / "scripts/claude-traffic"))
    stored = json.loads(zlib.decompress(db.execute("SELECT request_body FROM flows").fetchone()[0]))
    assert reader["restored"](db, stored)["system"] == SYSTEM


def test_rows_and_unused_blobs_older_than_traffic_days_are_dropped(tmp_path, monkeypatch):
    traffic = module.TrafficDB(lambda: str(tmp_path / "traffic.sqlite3"), lambda: 30)
    now = [1_000_000_000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(200, b"{}")
    traffic.response(flow)
    traffic.flush()
    now[0] += 31 * 86400
    flow = inference_flow()
    flow.request.content = json.dumps({"model": "m", "messages": [{"role": "user", "content": "new"}]}).encode()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(200, b"{}")
    traffic.response(flow)
    traffic.flush()
    db = sqlite3.connect(tmp_path / "traffic.sqlite3")
    assert db.execute("SELECT count(*) FROM flows").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 1  # only the new message


def test_reply_token_counts_are_stored_on_the_row(tmp_path):
    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    stream = (
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":3,"cache_creation_input_tokens":5,"cache_read_input_tokens":40,"output_tokens":1}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":9}}\n\n'
    )
    flow.response = http.Response.make(200, stream, {"content-type": "text/event-stream"})
    traffic.response(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]) == (8, 40, 9)


def test_a_reply_without_usage_has_unknown_not_zero_tokens(tmp_path):
    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(529, b'{"error": "overloaded"}')
    traffic.response(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]) == (None, None, None)


def test_older_databases_gain_the_token_columns(tmp_path):
    path = tmp_path / "traffic.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(module.SCHEMA.replace(
        ",\n    input_tokens INTEGER, cache_read_tokens INTEGER, output_tokens INTEGER", ""))
    old.close()
    recorder(tmp_path).connection()
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(flows)")}
    info = sqlite3.connect(path).execute("PRAGMA table_info(flows)").fetchall()
    assert {"input_tokens", "cache_read_tokens", "output_tokens", "api_key"} <= columns
    assert next(row[2] for row in info if row[1] == "api_key") == "TEXT"


def test_backfill_fills_old_rows_and_leaves_unreported_ones_unknown(tmp_path):
    cli = __import__("runpy").run_path(str(Path(__file__).parents[1] / "scripts/claude-traffic"))
    recorder(tmp_path).connection()
    db = sqlite3.connect(tmp_path / "traffic.sqlite3")
    reply = zlib.compress(b'{"usage": {"input_tokens": 2, "cache_read_input_tokens": 7, "output_tokens": 4}}')
    db.executemany("INSERT INTO flows (id, model, response_body) VALUES (?, ?, ?)", [
        ("a", "claude-opus-5-5", reply), ("b", "claude-opus-5-5", zlib.compress(b"overloaded")), ("c", None, reply)])
    db.commit()
    assert cli["backfill_tokens"](db, batch=1) == 1
    got = dict(db.execute("SELECT id, input_tokens FROM flows").fetchall())
    assert got == {"a": 2, "b": None, "c": None}
    assert db.execute("SELECT cache_read_tokens, output_tokens FROM flows WHERE id = 'a'").fetchone() == (7, 4)


def test_a_slow_database_never_blocks_the_event_loop(tmp_path, monkeypatch):
    import threading
    import time

    traffic = recorder(tmp_path)
    release = threading.Event()
    slow = traffic.write_flow
    monkeypatch.setattr(traffic, "write_flow", lambda meta: (release.wait(5), slow(meta)))
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(200, b"{}")
    started = time.monotonic()
    traffic.response(flow)
    assert time.monotonic() - started < 0.1
    release.set()
    traffic.flush()
    assert len(rows(tmp_path)) == 1


def test_a_full_queue_drops_rows_instead_of_waiting(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "QUEUE_MAX", 1)
    traffic = recorder(tmp_path)
    import threading

    release = threading.Event()
    monkeypatch.setattr(traffic, "write_flow", lambda meta: release.wait(5))
    for _ in range(3):
        flow = inference_flow()
        traffic.requestheaders(flow)
        flow.response = http.Response.make(200, b"{}")
        traffic.response(flow)
    assert traffic.dropped >= 1
    release.set()
    traffic.flush()
