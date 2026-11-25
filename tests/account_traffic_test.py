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


def test_account_index_does_not_wait_for_another_writer_and_restores_timeout(tmp_path):
    path = tmp_path / "locked.sqlite3"
    db = sqlite3.connect(path, isolation_level=None)
    db.executescript(module.SCHEMA)
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    started = module.time.monotonic()
    try:
        assert not module.account_index(db)
        assert module.time.monotonic() - started < 1
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        other.rollback()
        other.close()
        db.close()


def test_deferred_account_index_retries_on_a_later_idle_writer_tick(monkeypatch):
    recorder = module.TrafficDB.__new__(module.TrafficDB)
    recorder.db = sqlite3.connect(":memory:")
    recorder.index_pending, recorder.index_retry_at = True, 0
    attempts = []
    monkeypatch.setattr(module, "account_index", lambda db: attempts.append(db) or len(attempts) > 1)
    recorder.maintain_index()
    assert recorder.index_pending
    recorder.maintain_index()
    assert len(attempts) == 1, "a failed build must not spin on each flow"
    recorder.index_retry_at = 0
    recorder.maintain_index()
    assert not recorder.index_pending and len(attempts) == 2
    recorder.db.close()


def test_account_index_maintenance_is_bounded_and_an_abort_preserves_writes():
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.executescript(module.SCHEMA)
    db.executemany("INSERT INTO flows (id,status,served_by,started) VALUES (?,200,'a/model',?)",
                   [(str(i), i) for i in range(20_000)])
    assert not module.account_index(db, budget_s=-1)
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='flows_answered_account'").fetchone()
    db.execute("INSERT INTO flows (id) VALUES ('still-writable')")
    assert module.account_index(db)
    assert db.execute("SELECT count(*) FROM flows").fetchone()[0] == 20_001
    db.close()


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


def test_a_console_api_key_header_or_the_pool_carrying_it_is_never_stored():
    pool = '[{"alias":"console","kind":"api_key","api_key":"sk-ant-api03-fake-test-key"}]'
    assert module.clean_header("X-Api-Key", "sk-ant-api03-fake-test-key") == "[redacted]"
    stored = module.clean_headers([("x-api-key", "sk-ant-api03-fake-test-key"), ("x-claude-codex-account-pool", pool)])
    assert "sk-ant-api03-fake" not in stored


def test_cache_writes_and_their_one_hour_part_are_stored_for_pricing(tmp_path):
    tokens = module.reply_tokens(zlib.compress(json.dumps({"usage": {
        "input_tokens": 10, "cache_creation_input_tokens": 700, "cache_read_input_tokens": 5,
        "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 400},
        "output_tokens": 3}}).encode()))
    assert tokens == {"input": 710, "cache_read": 5, "output": 3, "cache_write": 700, "cache_write_1h": 400}
    assert {"cache_write_tokens", "cache_write_1h_tokens"} <= set(module.TOKEN_COLUMNS)


def test_first_byte_and_first_token_are_recorded_for_streams(tmp_path):
    import time

    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(200, b"", {"content-type": "text/event-stream"})
    flow.response.stream = True
    time.sleep(0.02)
    traffic.responseheaders(flow)
    flow.response.stream(b"event: message_start\n")
    time.sleep(0.02)
    before_delta = time.time()
    flow.response.stream(b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n')
    time.sleep(0.02)
    flow.response.stream(b"event: content_block_delta\n")
    traffic.response(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert row["started"] < row["first_byte"] < before_delta <= row["first_token"] < row["finished"]
    assert row["first_token"] - before_delta < 0.015, "the first delta, not a later one"


def test_a_reply_that_never_streamed_content_has_no_first_token(tmp_path):
    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(529, b'{"type":"error"}')
    traffic.responseheaders(flow)
    traffic.response(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert row["first_byte"] is not None and row["first_token"] is None


def test_open_connections_and_in_flight_calls_reach_the_live_table(tmp_path):

    traffic = recorder(tmp_path)
    client = tflow.tclient_conn()
    traffic.client_connected(client)
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(200, b"", {"x-claude-codex-served-by": "codex/sol"})
    traffic.responseheaders(flow)
    traffic.connection()
    traffic.write_live()
    [live] = rows(tmp_path, "live")
    body = json.loads(live["body"])
    assert body["connections"] == [client.peername[0]]
    [call] = body["inflight"]
    assert (call["served_by"], call["session_id"], call["host"]) == ("codex/sol", "s-1", "laptop")
    assert call["account"] == "proxy-account-user@example.com+rc=other"
    traffic.response(flow)
    traffic.client_disconnected(client)
    traffic.flush()
    traffic.write_live()
    body = json.loads(rows(tmp_path, "live")[0]["body"])
    assert body["connections"] == [] and body["inflight"] == []


def test_count_tokens_and_other_requests_are_not_in_flight_model_calls(tmp_path):
    traffic = recorder(tmp_path)
    flow = inference_flow()
    flow.request.path = "/v1/messages/count_tokens?beta=true"
    traffic.requestheaders(flow)
    assert traffic.inflight == {}


def test_older_databases_gain_the_timing_columns_and_live_table(tmp_path):
    db = sqlite3.connect(tmp_path / "traffic.sqlite3")
    old = module.SCHEMA.replace(", first_byte REAL, first_token REAL", "").split("CREATE TABLE IF NOT EXISTS live")[0]
    db.executescript(old)
    db.execute("INSERT INTO flows (id, started, model) VALUES ('old', 1, 'm')")
    db.commit()
    db.close()
    recorder(tmp_path).connection()
    db = sqlite3.connect(tmp_path / "traffic.sqlite3")
    assert {"first_byte", "first_token"} <= {row[1] for row in db.execute("PRAGMA table_info(flows)")}
    assert db.execute("SELECT first_token FROM flows WHERE id = 'old'").fetchone() == (None,)
    assert db.execute("SELECT count(*) FROM live").fetchone() == (0,)


def test_a_call_that_never_finished_drops_out_of_the_live_table_after_an_hour(tmp_path):
    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    traffic.inflight[flow.id]["started"] -= module.INFLIGHT_MAX_S + 1
    traffic.connection()
    traffic.write_live()
    assert json.loads(rows(tmp_path, "live")[0]["body"])["inflight"] == [] and traffic.inflight == {}


def test_first_token_is_found_inside_a_gzip_stream(tmp_path):
    import gzip
    import time

    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(200, b"", {"content-type": "text/event-stream", "content-encoding": "gzip"})
    flow.response.stream = True
    traffic.responseheaders(flow)
    raw = gzip.compress(b"event: message_start\ndata: {}\n\n" + b"event: content_block_delta\ndata: {}\n\n" * 50)
    flow.response.stream(raw[:20])
    time.sleep(0.02)
    before = time.time()
    flow.response.stream(raw[20:])
    traffic.response(flow)
    traffic.flush()
    [row] = rows(tmp_path)
    assert row["first_token"] >= before
    assert zlib.decompress(row["response_body"]).startswith(b"event: message_start")


def test_an_undecodable_gzip_stream_has_unknown_first_token_and_still_passes_through(tmp_path):
    traffic = recorder(tmp_path)
    flow = inference_flow()
    traffic.requestheaders(flow)
    flow.response = http.Response.make(200, b"", {"content-type": "text/event-stream", "content-encoding": "gzip"})
    flow.response.stream = True
    traffic.responseheaders(flow)
    assert flow.response.stream(b"not gzip at all") == b"not gzip at all"
    assert flow.response.stream(b"content_block_delta") == b"content_block_delta"
    traffic.response(flow)
    traffic.flush()
    assert rows(tmp_path)[0]["first_token"] is None
