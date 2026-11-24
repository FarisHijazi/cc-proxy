"""A Console key's month-to-date spend: official prices times each reply's exact usage."""

import calendar
import json
import runpy
import sqlite3
import zlib
from pathlib import Path

import pytest

spend = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_key_spend.py"))
NOW = calendar.timegm((2026, 11, 24, 12, 0, 0))


def test_prices_match_the_longest_id_and_dated_ids_and_haiku_long_prompts():
    assert spend["price"]("claude-opus-5-5")["input"] == 4
    assert spend["price"]("claude-opus-5")["input"] == 5
    assert spend["price"]("claude-haiku-4-5-20251001")["output"] == 5
    assert spend["price"]("claude-haiku-5-5", 100_001)["input"] == 0.5
    assert spend["price"]("claude-haiku-5-5", 100_000)["input"] == 0.1
    assert spend["price"]("claude-opus-5-55") is None


def test_a_reply_is_priced_per_token_kind():
    tokens = {"input": 1_000, "cache_write": 800, "cache_write_1h": 300, "cache_read": 5_000, "output": 100}
    # opus 5.5: 200 uncached x4 + 500 5m x5 + 300 1h x8 + 5000 read x0.2 + 100 out x20, per MTok
    assert spend["reply_cost"]("claude-opus-5-5", tokens) == pytest.approx((800 + 2500 + 2400 + 1000 + 2000) / 1e6)
    dearest = spend["reply_cost"]("claude-unknown-9", tokens)
    assert dearest >= spend["reply_cost"]("claude-fable-5-1", tokens), "an unknown model is never cheap"


def test_the_month_starts_on_the_first_at_midnight_utc():
    assert spend["month_start"](NOW) == calendar.timegm((2026, 11, 1, 0, 0, 0))


def flows(tmp_path):
    db = sqlite3.connect(tmp_path / "traffic.sqlite3")
    db.execute("CREATE TABLE flows (started REAL, status INTEGER, served_by TEXT, url TEXT, input_tokens INTEGER, "
               "cache_read_tokens INTEGER, output_tokens INTEGER, cache_write_tokens INTEGER, "
               "cache_write_1h_tokens INTEGER, response_body BLOB)")
    return db


def test_month_spend_counts_only_this_months_answered_messages_of_that_key(tmp_path):
    db = flows(tmp_path)
    url = "https://api.anthropic.com/v1/messages?beta=true"
    reply = zlib.compress(json.dumps({"usage": {"input_tokens": 0, "cache_creation_input_tokens": 1_000_000,
                                                "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000},
                                                "cache_read_input_tokens": 0, "output_tokens": 0}}).encode())
    rows = [
        (NOW - 60, 200, "key_a/claude-sonnet-5-5", url, 0, 0, 1_000_000, 0, 0, None),   # $10 output
        (NOW - 30, 200, "key_a/claude-sonnet-5-5", url, None, None, None, None, None, reply),  # $4: 1M 1h writes, old row
        (NOW - 30, 400, "key_a/claude-sonnet-5-5", url, 0, 0, 1_000_000, 0, 0, None),   # refused: free
        (NOW - 30, 200, "key_a/claude-sonnet-5-5", url.replace("messages", "messages/count_tokens"), 9, 0, 9, 0, 0, None),
        (NOW - 30, 200, "keyXa/claude-sonnet-5-5", url, 0, 0, 1_000_000, 0, 0, None),   # another alias (LIKE escape)
        (calendar.timegm((2026, 10, 31, 23, 59, 0)), 200, "key_a/claude-sonnet-5-5", url, 0, 0, 1_000_000, 0, 0, None),
    ]
    db.executemany("INSERT INTO flows VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    assert spend["month_spend"](db, "key_a", NOW) == pytest.approx(14.0)
