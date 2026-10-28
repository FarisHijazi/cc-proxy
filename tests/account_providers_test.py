"""Provider cards' recorded usage: per-backend counts and tokens from the traffic database."""

import json
import runpy
import sqlite3
import zlib
from pathlib import Path

providers = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_providers.py"), run_name="account_providers")
NOW = 1_800_000_000.0
MESSAGES = "https://api.anthropic.com/v1/messages?beta=true"


def sse(*events):
    return zlib.compress("".join(f"event: x\ndata: {json.dumps(e)}\n\n" for e in events).encode())


def database(rows):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE flows (started REAL, served_by TEXT, model TEXT, status INTEGER, response_body BLOB, url TEXT)")
    db.executemany("INSERT INTO flows VALUES (?,?,?,?,?,?)", [(*row, MESSAGES)[:6] for row in rows])
    db.commit()
    return db


STREAMED = sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 100, "output_tokens": 0}}},
    {"type": "message_delta", "usage": {"input_tokens": 40, "cache_read_input_tokens": 60, "output_tokens": 7}},
)


def test_sse_usage_takes_the_final_delta_over_message_start():
    assert providers["reply_tokens"](STREAMED) == {"input": 40, "cache_read": 60, "output": 7}


def test_plain_json_reply_and_reply_without_usage():
    body = zlib.compress(json.dumps({"usage": {"input_tokens": 3, "output_tokens": 2}}).encode())
    assert providers["reply_tokens"](body) == {"input": 3, "cache_read": 0, "output": 2}
    assert providers["reply_tokens"](zlib.compress(b"upstream error")) is None
    assert providers["reply_tokens"](None) is None


def test_counts_each_backend_in_the_window_and_skips_claude_accounts():
    db = database([
        (NOW - 60, "codex/gpt-6.1-sol", "claude-opus-5", 200, STREAMED),
        (NOW - 120, "codex/gpt-6.1-luna", "gpt-6.1-luna", 429, None),
        (NOW - 30, "user@example.com/claude-opus-5", "claude-opus-5", 200, STREAMED),
        (NOW - 90, "gemini/gemini-3-pro", "gemini-3-pro", 200, STREAMED),
        (NOW - 25 * 3600, "cursor-cli/cursor-cli:auto", "cursor-cli:auto", 200, STREAMED),
    ])
    usage = providers["provider_usage"](db, now=NOW)
    codex, gemini, cursor_cli = (usage["providers"][n] for n in ("codex", "gemini", "cursor-cli"))
    assert usage["source"] == "traffic database" and usage["hours"] == 24 and usage["at"] == NOW
    assert "user@example.com" not in usage["providers"]
    assert (codex["requests"], codex["errors"], codex["last_model"], codex["last_status"]) == (2, 1, "gpt-6.1-sol", 200)
    assert codex["tokens"] == {"input": 40, "cache_read": 60, "output": 7} and codex["tokens_reported"] == 1
    assert gemini["requests"] == 1 and gemini["tokens_reported"] == 1
    assert cursor_cli["requests"] == 0 and cursor_cli["last_at"] is None
    assert not usage["tokens_capped"]


def test_untagged_rows_are_classified_by_the_requested_model():
    db = database([
        (NOW - 10, None, "cursor-cli:claude-4.6-opus", 200, STREAMED),
        (NOW - 20, None, "cursor-cli-agent:gpt-6", 500, None),
        (NOW - 30, None, "cursor-agent", 200, None),
        (NOW - 40, None, "cursor:claude-4.6-opus-high-thinking", 200, None),
        (NOW - 50, None, "gemini-3-pro[1m]", 200, STREAMED),
        (NOW - 60, None, "kimi-k3", 200, None),
        (NOW - 70, None, "grok-4.5", 200, None),
        (NOW - 80, None, "claude-opus-5", 400, None),
        (NOW - 90, None, "opus", 200, None),
        (NOW - 95, None, "made-up-model", 400, None),
        (NOW - 5, None, "gemini-3-pro", 200, None, "https://api.anthropic.com/v1/messages/count_tokens?beta=true"),
        (NOW - 1, "gemini/gemini-3-flash", "claude-sonnet-5", 200, None),
    ])
    found = providers["provider_usage"](db, now=NOW)["providers"]
    assert {name: card["requests"] for name, card in found.items()} == {
        "codex": 0, "cursor-cli": 2, "cursor": 2, "gemini": 2, "kimi": 1, "grok": 1}
    assert found["cursor-cli"]["errors"] == 1 and found["cursor-cli"]["last_model"] == "cursor-cli:claude-4.6-opus"
    assert found["gemini"]["last_model"] == "gemini-3-flash", "the router's served_by wins over the requested id"
    assert found["gemini"]["tokens_reported"] == 1


def test_an_unreadable_body_skips_its_tokens_only():
    db = database([(NOW - 1, "codex/m", "m", 200, b"not zlib"), (NOW - 2, "codex/m", "m", 200, STREAMED)])
    codex = providers["provider_usage"](db, now=NOW)["providers"]["codex"]
    assert codex["requests"] == 2 and codex["tokens_reported"] == 1


def test_token_decoding_is_capped_but_counts_cover_the_window():
    db = database([(NOW - i, "codex/m", "m", 200, STREAMED) for i in range(1, 6)])
    usage = providers["provider_usage"](db, now=NOW, limit=2)
    assert usage["providers"]["codex"]["requests"] == 5
    assert usage["providers"]["codex"]["tokens_reported"] == 2 and usage["tokens_capped"]


def test_no_database_is_unknown_not_zero():
    assert providers["provider_usage"](None) is None


def test_dashboard_reads_the_configured_traffic_database(tmp_path):
    ui = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_ui.py"), run_name="account_ui")
    path = tmp_path / "traffic.db"
    database([(1.0, "codex/m", "m", 200, None)]).execute(f"VACUUM INTO '{path}'")
    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.cache = {}
    usage = board.recorded_provider_usage({"traffic_db": str(path)})
    assert usage["providers"]["codex"]["requests"] == 0, "a call outside the window is not counted"
    assert board.recorded_provider_usage({}) is usage, "cached for the refresh interval"
    board.cache = {}
    assert board.recorded_provider_usage({}) is None, "no traffic database is unknown"


CURSOR = {"source": "cursor DashboardService (cursor-agent login)", "at": NOW, "plan": "Pro", "period_start": NOW - 9 * 86400,
          "resets_at": NOW + 21 * 86400, "message": None,
          "included": {"used_cents": 1825, "limit_cents": 2000, "remaining_cents": 175, "spend_percent": 91.25,
                       "total_percent": 3.69, "auto_percent": 0.51, "api_percent": 35.42},
          "on_demand": {"enabled": False, "used_cents": 0, "limit_cents": None}}


def test_cursor_snapshot_becomes_a_dollar_window_with_its_source():
    card = providers["cursor_card_quota"](CURSOR)
    assert card["source"] == CURSOR["source"] and card["at"] == NOW
    assert card["windows"] == [
        {"label": "Included", "used": 18.25, "limit": 20.0, "unit": "USD", "resets_at": "2027-02-05T08:00:00+00:00"},
        {"label": "API", "used": 35.42, "limit": 100, "unit": "%", "resets_at": None},
        {"label": "Auto", "used": 0.51, "limit": 100, "unit": "%", "resets_at": None},
    ]
    assert "Pro plan" in card["note"] and "/usage total 4%" in card["note"] and "on-demand usage off" in card["note"]


def test_cursor_on_demand_without_a_cap_has_no_limit():
    snapshot = dict(CURSOR, included=None, on_demand={"enabled": True, "used_cents": 250, "limit_cents": None})
    assert providers["cursor_card_quota"](snapshot)["windows"][0] == {
        "label": "On-demand", "used": 2.5, "limit": None, "unit": "USD", "resets_at": "2027-02-05T08:00:00+00:00"}


def test_cursor_unknown_and_errors_pass_through():
    assert providers["cursor_card_quota"](None) is None
    assert providers["cursor_card_quota"]({"error": "GetCurrentPeriodUsage HTTP 401"}) == {"error": "GetCurrentPeriodUsage HTTP 401"}


def test_dashboard_caches_the_cursor_read_on_the_cli_card_only():
    ui = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_ui.py"), run_name="account_ui")
    calls = []
    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.cache = {}
    board.provider_quota.__func__.__globals__["cursor_quota"] = lambda: calls.append(1) or CURSOR
    board.provider_quota.__func__.__globals__["codex_quota"] = lambda: None
    first = board.provider_quota()
    assert sorted(first) == ["codex", "cursor-cli"] and first["codex"] is None
    assert first["cursor-cli"]["windows"][0]["used"] == 18.25
    assert board.provider_quota() is first and calls == [1]


def test_malformed_cursor_fields_are_isolated_and_cached():
    ui = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_ui.py"), run_name="account_ui")
    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.cache = {}
    calls = []
    bad = {**CURSOR, "included": {**CURSOR["included"], "total_percent": "private-value"}}
    board.provider_quota.__func__.__globals__["cursor_quota"] = lambda: calls.append(1) or bad
    board.provider_quota.__func__.__globals__["codex_quota"] = lambda: 1 / 0
    first = board.provider_quota()
    assert first == {"codex": {"error": "codex quota: ZeroDivisionError"},
                     "cursor-cli": {"error": "cursor-cli quota: ValueError"}}
    assert "private-value" not in json.dumps(first)
    assert board.provider_quota() is first and calls == [1]
