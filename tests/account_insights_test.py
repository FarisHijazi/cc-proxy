"""Dashboard insights: load, speed and health from the traffic database, overall and per account and model."""

import json
import runpy
import sqlite3
from pathlib import Path

import pytest

scripts = Path(__file__).parents[1] / "scripts"
traffic = runpy.run_path(str(scripts / "account_traffic.py"))
insights = runpy.run_path(str(scripts / "account_insights.py"))["insights"]
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


@pytest.fixture
def db():
    db = sqlite3.connect(":memory:")
    db.executescript(traffic["SCHEMA"])
    return db


def test_totals_speed_and_ttft_overall(db):
    flow(db, "1", 60)
    flow(db, "2", 120, session="s2", host="box", ttft=3.0, duration=13.0, tokens=(100, 0, 1000))
    flow(db, "3", 2000, session="s3", status=429, ttft=None, tokens=(None, None, None))
    result = insights(db, 1, NOW)
    t = result["total"]
    assert (t["requests"], t["errors"], t["error_rate"]) == (3, 1, pytest.approx(1 / 3))
    assert t["per_minute"] == pytest.approx(3 / 60)
    assert (t["sessions"], t["active_sessions"], t["machines"], t["active_machines"]) == (3, 2, 2, 2)
    assert t["statuses"] == {"200": 2, "429": 1}
    assert t["ttft"] == {"now": 1.0, "p50": 1.0, "p95": 3.0, "samples": 2}
    assert (t["speed"]["p50"], t["speed"]["p5"]) == (50.0, 50.0)  # 500 tok / 10 s and 1000 tok / 10 s
    assert t["throughput_now"] == pytest.approx(1500 / 300)
    assert t["tokens"] == {"input": 200, "cache_read": 900, "output": 1500}
    assert t["cache_hit"] == pytest.approx(900 / 1100)
    assert t["tokens_reported"] == 2


def test_breakdowns_per_account_and_model_and_fallback_rate(db):
    flow(db, "1", 60)
    flow(db, "2", 60, served="codex/gpt-6.1-sol", model="claude-opus-5-5", account="proxy-account-a@example.com")
    flow(db, "3", 60, served="codex/sol", model="sol[1m]")
    flow(db, "4", 60, served=None, status=400, model="sol", ttft=None, tokens=(None, None, None))
    result = insights(db, 1, NOW)
    accounts = {e["account"]: e for e in result["by_account"]}
    assert {name: e["requests"] for name, e in accounts.items()} == {"a@example.com": 2, "codex": 2}
    models = {e["model"]: e["requests"] for e in result["by_model"]}
    assert models == {"claude-opus-5-5": 1, "gpt-6.1-sol": 1, "sol": 2}
    assert result["total"]["fallbacks"] == 1, "only the Opus call answered by Sol changed model; [1m] is a hint"
    assert accounts["codex"]["fallback_rate"] == 0.5


def test_count_tokens_old_rows_and_other_windows_are_left_out(db):
    flow(db, "1", 60, url="https://api.anthropic.com/v1/messages/count_tokens?beta=true")
    flow(db, "2", 7200)
    result = insights(db, 1, NOW)
    assert result["total"]["requests"] == 0
    assert result["by_account"] == [] and result["by_model"] == []
    assert insights(db, 24, NOW)["total"]["requests"] == 1


def test_no_samples_are_unknown_not_zero(db):
    flow(db, "1", 60, ttft=None, tokens=(None, None, None))
    t = insights(db, 1, NOW)["total"]
    assert t["ttft"]["p50"] is None and t["ttft"]["now"] is None and t["speed"]["p50"] is None
    assert t["tokens"] is None and t["cache_hit"] is None and t["throughput_now"] is None
    empty = insights(db, 1, NOW + 7200)["total"]
    assert empty["error_rate"] is None and empty["fallback_rate"] is None


def test_live_connections_and_in_flight_calls_per_account_and_model(db):
    flow(db, "1", 60)
    inflight = [{"account": "proxy-account-a@example.com", "served_by": None, "started": NOW - 2},
                {"account": "proxy-account-a@example.com", "served_by": "codex/sol", "started": NOW - 1}]
    db.execute("INSERT INTO live VALUES ('now', ?, ?)",
               (NOW - 3, json.dumps({"connections": ["100.64.0.1", "100.64.0.1", "100.64.0.2"], "inflight": inflight})))
    result = insights(db, 1, NOW)
    assert result["live"]["connections"] == 3 and result["live"]["client_ips"] == 2 and result["live"]["inflight"] == 2
    accounts = {e["account"]: e["inflight"] for e in result["by_account"]}
    assert accounts == {"a@example.com": 1, "codex": 1}
    assert {e["model"]: e["inflight"] for e in result["by_model"]} == {"claude-opus-5-5": 0, "sol": 1}
    assert result["total"]["inflight"] == 2


def test_a_stale_or_missing_live_snapshot_is_unknown(db):
    db.execute("INSERT INTO live VALUES ('now', ?, ?)", (NOW - 120, json.dumps({"connections": [], "inflight": []})))
    assert insights(db, 1, NOW)["live"] is None
    old = sqlite3.connect(":memory:")
    old.execute("CREATE TABLE flows (id TEXT, started REAL, finished REAL, session_id TEXT, client_host TEXT, "
                "client_ip TEXT, status INTEGER, model TEXT, served_by TEXT, account TEXT, url TEXT)")
    old.execute("INSERT INTO flows VALUES ('1', ?, ?, 's', 'h', NULL, 200, 'm', 'a/m', NULL, 'u')", (NOW - 60, NOW - 50))
    result = insights(old, 1, NOW)
    assert result["live"] is None and result["timings_recorded"] is False
    assert result["total"]["requests"] == 1 and result["total"]["ttft"]["p50"] is None and result["total"]["tokens"] is None


@pytest.mark.parametrize("minutes", [1, 5, 15])
def test_short_windows_and_bucket_medians(db, minutes):
    flow(db, "in", minutes * 60 - 1, duration=0.5, ttft=0.1, tokens=(10, 0, 20))
    flow(db, "out", minutes * 60 + 1)
    result = insights(db, minutes / 60, NOW)
    assert result["total"]["requests"] == 1
    assert result["total"]["per_minute"] == pytest.approx(1 / minutes)
    history = result["history"]
    assert len(history["points"]) == 30
    assert history["bucket_seconds"] == pytest.approx(minutes * 2)
    assert history["points"][0]["speed"] == pytest.approx(50)
    assert history["points"][0]["ttft"] == pytest.approx(0.1)
    assert history["points"][-1]["speed"] is None
    assert history["points"][-1]["speed_samples"] == 0


@pytest.mark.parametrize("recent_speed, expected", [(50, "healthy"), (30, "degraded"), (10, "slow")])
def test_model_relative_health_excludes_recent_from_baseline(db, recent_speed, expected):
    for i in range(5):
        flow(db, str(i), 600 + i * 20)
    flow(db, "recent", 60, tokens=(100, 0, recent_speed * 10))
    result = insights(db, 1, NOW)
    health = result["health"]["models"][0]
    assert health["speed"]["state"] == expected
    assert health["speed"]["baseline"] == 50
    assert health["speed"]["baseline_samples"] == 5
    assert health["speed"]["samples"] == 1
    assert health["ttft"]["state"] == "healthy"
    assert health["speed"]["ratio"] == pytest.approx(recent_speed / 50)


def test_health_unknown_without_recent_or_sufficient_baseline(db):
    flow(db, "old", 600)
    result = insights(db, 1, NOW)
    h = result["health"]["models"][0]["speed"]
    assert h["state"] == "unknown" and h["ratio"] is None and h["current"] is None
    flow(db, "new", 60)
    assert insights(db, 1, NOW)["health"]["models"][0]["speed"]["state"] == "unknown"


def test_filters_use_answering_account_and_model_for_every_metric(db):
    flow(db, "a", 60)
    flow(db, "b", 60, served="codex/sol")
    result = insights(db, 1, NOW, account="codex", model="sol")
    assert result["total"]["requests"] == 1
    assert [g["account"] for g in result["by_account"]] == ["codex"]
    assert [g["model"] for g in result["by_model"]] == ["sol"]
    assert [g["model"] for g in result["health"]["models"]] == ["sol"]
    assert result["filters"]["accounts"] == ["a@example.com", "codex"]
    assert sum(p["speed_samples"] for p in result["history"]["points"]) == 1
    assert insights(db, 1, NOW, account="missing")["health"]["models"] == []


def test_failed_nonstream_short_and_future_timings_are_not_history_samples(db):
    flow(db, "failed", 60, status=500)
    flow(db, "nonstream", 60, ttft=None)
    flow(db, "burst", 60, ttft=1, duration=1.01)
    flow(db, "pending", 1)
    result = insights(db, 1, NOW)
    assert sum(p["speed_samples"] for p in result["history"]["points"]) == 0
    assert sum(p["ttft_samples"] for p in result["history"]["points"]) == 1


@pytest.mark.parametrize("hours", [0, -1, 25, float("nan"), float("inf")])
def test_invalid_ranges_are_rejected(db, hours):
    with pytest.raises(ValueError):
        insights(db, hours, NOW)


def test_history_query_uses_started_index_and_never_reads_bodies(db):
    queries = []
    db.set_trace_callback(queries.append)
    insights(db, 1 / 60, NOW)
    query = next(q for q in queries if "FROM flows WHERE" in q)
    plan = db.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("USING INDEX flows_started" in row[-1] for row in plan)
    assert "request_body" not in query and "response_body" not in query


def test_zero_ttft_baseline_is_measured_not_missing(db):
    for i in range(6):
        flow(db, str(i), 60 if i == 5 else 600 + i * 20)
    db.execute("UPDATE flows SET first_token = started")
    h = insights(db, 1, NOW)["health"]["models"][0]["ttft"]
    assert h["state"] == "healthy" and h["baseline"] == 0 and h["current"] == 0


def test_live_only_filter_identities_are_available(db):
    db.execute("INSERT INTO live VALUES ('now', ?, ?)", (NOW, json.dumps({"connections": [], "inflight": [
        {"account": "proxy-account-live@example.com", "served_by": "codex/sol"}]})))
    result = insights(db, 1, NOW)
    assert result["filters"] == {"accounts": ["codex"], "models": ["sol"]}


def test_future_started_rows_do_not_inflate_any_metrics(db):
    flow(db, "future", -60)
    result = insights(db, 1, NOW)
    assert result["total"]["requests"] == 0
    assert result["health"]["models"] == []
