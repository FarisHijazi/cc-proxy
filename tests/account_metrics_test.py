"""Prometheus /metrics: the insights traffic as counters, histograms and gauges, read incrementally from the DB."""

import json
import runpy
import sqlite3
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families

scripts = Path(__file__).parents[1] / "scripts"
traffic = runpy.run_path(str(scripts / "account_traffic.py"))
Metrics = runpy.run_path(str(scripts / "account_metrics.py"))["Metrics"]
NOW = 1_000_000.0


def flow(db, id, ago, *, session="s1", host="laptop", model="claude-opus-5-5", served="a@example.com/claude-opus-5-5",
         status=200, account="proxy-account-a@example.com", ttft=1.0, duration=11.0, tokens=(100, 900, 500),
         url="https://api.anthropic.com/v1/messages?beta=true"):
    started = NOW - ago
    db.execute(
        "INSERT INTO flows (id, started, finished, first_byte, first_token, session_id, client_host, model, served_by, "
        "status, account, url, input_tokens, cache_read_tokens, output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (id, started, started + duration, started + (ttft or 0.5), started + ttft if ttft else None, session, host,
         model, served, status, account, url, *tokens))


def live(db, at, inflight=(), connections=()):
    db.execute("INSERT OR REPLACE INTO live VALUES ('now', ?, ?)",
               (at, json.dumps({"connections": list(connections), "inflight": list(inflight), "dropped": 0})))


def samples(text):
    """{(sample name, frozenset(labels)): value}, parsed by the official client so the format is checked too."""
    return {(s.name, frozenset(s.labels.items())): s.value
            for family in text_string_to_metric_families(text) for s in family.samples}


def value(parsed, name, **labels):
    return parsed.get((name, frozenset(labels.items())))


@pytest.fixture
def db():
    db = sqlite3.connect(":memory:")
    db.executescript(traffic["SCHEMA"])
    return db


def test_counts_by_account_model_and_status(db):
    flow(db, "1", 60)
    flow(db, "2", 60, served="codex/gpt-6.1-sol", tokens=(10, 0, 20))
    flow(db, "3", 60, served=None, status=429, model="sol", ttft=None, tokens=(None, None, None))
    flow(db, "4", 60, url="https://api.anthropic.com/v1/messages/count_tokens?beta=true")
    m = samples(Metrics().scrape(db, NOW))
    a, codex = {"account": "a@example.com", "model": "claude-opus-5-5"}, {"account": "codex", "model": "gpt-6.1-sol"}
    assert value(m, "cc_proxy_requests_total", **a, status="200") == 1
    assert value(m, "cc_proxy_requests_total", **codex, status="200") == 1
    assert value(m, "cc_proxy_requests_total", account="a@example.com", model="sol", status="429") == 1
    assert value(m, "cc_proxy_errors_total", account="a@example.com", model="sol") == 1
    assert value(m, "cc_proxy_fallbacks_total", **codex) == 1, "Opus asked, Sol answered"
    assert value(m, "cc_proxy_fallbacks_total", **a) == 0
    assert value(m, "cc_proxy_tokens_total", **a, type="cache_read") == 900
    assert value(m, "cc_proxy_tokens_total", **codex, type="output") == 20
    assert value(m, "cc_proxy_tokens_total", account="a@example.com", model="sol", type="output") is None
    assert sum(v for (name, _), v in m.items() if name == "cc_proxy_requests_total") == 3, "count_tokens is left out"


def test_histograms_hold_ttft_and_speed(db):
    flow(db, "1", 60, ttft=1.0, duration=11.0, tokens=(1, 0, 500))  # 50 tok/s
    flow(db, "2", 60, ttft=4.0, duration=14.0, tokens=(1, 0, 1000))  # 100 tok/s
    m = samples(Metrics().scrape(db, NOW))
    a = {"account": "a@example.com", "model": "claude-opus-5-5"}
    assert value(m, "cc_proxy_ttft_seconds_count", **a) == 2
    assert value(m, "cc_proxy_ttft_seconds_sum", **a) == 5.0
    assert value(m, "cc_proxy_ttft_seconds_bucket", **a, le="1") == 1
    assert value(m, "cc_proxy_ttft_seconds_bucket", **a, le="5") == 2
    assert value(m, "cc_proxy_ttft_seconds_bucket", **a, le="+Inf") == 2
    assert value(m, "cc_proxy_output_tokens_per_second_bucket", **a, le="50") == 1
    assert value(m, "cc_proxy_output_tokens_per_second_bucket", **a, le="100") == 2


def test_later_scrapes_count_only_new_rows_and_start_from_the_active_window(db):
    flow(db, "old", 3600)  # before this process's window: never counted
    flow(db, "1", 60)
    metrics = Metrics()
    a = {"account": "a@example.com", "model": "claude-opus-5-5", "status": "200"}
    assert value(samples(metrics.scrape(db, NOW)), "cc_proxy_requests_total", **a) == 1
    assert value(samples(metrics.scrape(db, NOW)), "cc_proxy_requests_total", **a) == 1, "no double counting"
    flow(db, "2", 1)
    assert value(samples(metrics.scrape(db, NOW)), "cc_proxy_requests_total", **a) == 2


def test_active_sessions_and_machines_expire(db):
    flow(db, "1", 60, session="s1", host="laptop")
    flow(db, "2", 60, session="s2", host="box", served="codex/gpt-6.1-sol")
    metrics = Metrics()
    m = samples(metrics.scrape(db, NOW))
    assert value(m, "cc_proxy_active_sessions", account="a@example.com") == 1
    assert value(m, "cc_proxy_active_sessions", account="codex") == 1
    assert value(m, "cc_proxy_active_machines") == 2
    later = samples(metrics.scrape(db, NOW + 3600))
    assert value(later, "cc_proxy_active_machines") == 0
    assert value(later, "cc_proxy_active_sessions", account="codex") == 0


def test_live_gauges_and_stale_snapshot_is_unknown(db):
    flow(db, "1", 60)
    live(db, NOW - 2, inflight=[{"served_by": "a@example.com/claude-opus-5-5", "account": "proxy-account-a@example.com"},
                                {"served_by": None, "account": "proxy-account-b@example.com"}],
         connections=["100.64.0.1", "100.64.0.1", "100.64.0.2"])
    metrics = Metrics()
    m = samples(metrics.scrape(db, NOW))
    assert value(m, "cc_proxy_inflight", account="a@example.com", model="claude-opus-5-5") == 1
    assert value(m, "cc_proxy_inflight", account="b@example.com", model="unknown") == 1
    assert (value(m, "cc_proxy_open_connections"), value(m, "cc_proxy_connected_client_ips")) == (3, 2)
    assert value(m, "cc_proxy_recorder_up") == 1
    stale = samples(metrics.scrape(db, NOW + 120))
    assert value(stale, "cc_proxy_recorder_up") == 0
    assert value(stale, "cc_proxy_open_connections") is None, "a stale snapshot is unknown, not zero"
    assert value(stale, "cc_proxy_inflight", account="a@example.com", model="claude-opus-5-5") is None


def test_idle_pairs_read_zero_in_flight(db):
    flow(db, "1", 60)
    live(db, NOW)
    m = samples(Metrics().scrape(db, NOW))
    assert value(m, "cc_proxy_inflight", account="a@example.com", model="claude-opus-5-5") == 0


def test_label_values_are_escaped(db):
    flow(db, "1", 60, served='x"y\\z/m\nn')
    m = samples(Metrics().scrape(db, NOW))
    assert value(m, "cc_proxy_requests_total", account='x"y\\z', model="m\nn", status="200") == 1


def test_no_database_still_answers(db):
    m = samples(Metrics().scrape(None, NOW))
    assert value(m, "cc_proxy_traffic_db_up") == 0 and value(m, "cc_proxy_recorder_up") == 0


def test_served_over_http_by_the_dashboard(tmp_path):
    ui = runpy.run_path(str(scripts / "account_ui.py"), run_name="account_ui")
    path = tmp_path / "traffic.db"
    db = sqlite3.connect(path)
    db.executescript(traffic["SCHEMA"])
    flow(db, "1", NOW - time.time())  # now: the dashboard counts from the real clock
    db.commit()
    db.close()
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"accounts": {}, "traffic_db": str(path)}))
    board = ui["Dashboard"](accounts, "http://127.0.0.1:9", Path("/etc/ssl/certs/ca-certificates.crt"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui["handler"](board))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/metrics") as response:
            kind, text = response.headers["content-type"], response.read().decode()
    finally:
        server.shutdown()
    assert kind.startswith("text/plain; version=0.0.4")
    m = samples(text)
    assert value(m, "cc_proxy_traffic_db_up") == 1
    assert value(m, "cc_proxy_requests_total", account="a@example.com", model="claude-opus-5-5", status="200") == 1


def test_account_model_cardinality_is_capped(db):
    for i in range(300):
        flow(db, str(i), 60, served=f"account-{i}/model-{i}")
    m = samples(Metrics().scrape(db, NOW))
    request_samples = [(key, v) for (name, key), v in m.items() if name == "cc_proxy_requests_total"]
    assert len(request_samples) == 257
    assert value(m, "cc_proxy_requests_total", account="other", model="other", status="200") == 44
    assert sum(v for _, v in request_samples) == 300


def test_new_flow_can_finish_after_the_bootstrap_window(db):
    flow(db, "1", 60)
    metrics = Metrics()
    metrics.scrape(db, NOW)
    flow(db, "late", 3600)  # a long-running call only just recorded: rowid, not started, is the cursor
    m = samples(metrics.scrape(db, NOW))
    assert value(m, "cc_proxy_requests_total", account="a@example.com", model="claude-opus-5-5", status="200") == 2


def test_pruning_does_not_replay_retained_rows(db):
    flow(db, "retained", 60)
    flow(db, "late", 30)
    metrics = Metrics()
    metrics.scrape(db, NOW)
    db.execute("DELETE FROM flows WHERE id = 'late'")
    m = samples(metrics.scrape(db, NOW))
    assert value(m, "cc_proxy_requests_total", account="a@example.com", model="claude-opus-5-5", status="200") == 2
