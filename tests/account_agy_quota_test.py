"""agy quota read helper: parsing of real Cloud Code replies, and unknown vs error."""

import json
import runpy
import urllib.error
from pathlib import Path

quota = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_agy_quota.py"), run_name="account_agy_quota")
NOW = 1_795_530_000.0

# Captured from a live Antigravity Starter account (2026-11-24), per-model list trimmed.
SUMMARY = {"groups": [{"buckets": [
    {"bucketId": "3p-weekly", "displayName": "Weekly Limit Remaining", "window": "weekly",
     "resetTime": "2026-12-01T13:57:43Z", "remainingFraction": 0.916},
    {"bucketId": "3p-5h", "displayName": "Five Hour Limit Remaining", "window": "5h",
     "resetTime": "2026-11-24T18:57:43Z", "remainingFraction": 0.916}],
    "displayName": "Claude and GPT models",
    "description": "Models within this group: Claude Sonnet 4.6, Claude Opus 4.6, GPT-OSS 120B"}],
    "description": "Within each group, models share a weekly limit and a 5-hour limit."}
PER_MODEL = {"buckets": [
    {"resetTime": "2026-12-01T13:57:43Z", "tokenType": "WTUS", "modelId": "claude-sonnet-4-6", "remainingFraction": 0.916},
    {"resetTime": "2026-12-01T13:19:43Z", "tokenType": "WTUS", "modelId": "gemini-3-flash", "remainingFraction": 0.74},
    {"resetTime": "2026-12-01T13:19:43Z", "tokenType": "WTUS", "modelId": "gemini-3.8-flash-tiered", "remainingFraction": 0.74},
    {"tokenType": "WTUS", "modelId": "tab_flash_lite_preview", "remainingFraction": 1}]}


def test_parses_live_starter_replies():
    q = quota["parse"](SUMMARY, PER_MODEL, NOW)
    assert q["at"] == NOW and q["source"].startswith("Antigravity")
    assert q["windows"] == [
        {"label": "Claude and GPT models · weekly", "used": 8.4, "limit": 100, "unit": "%", "resets_at": "2026-12-01T13:57:43Z"},
        {"label": "Claude and GPT models · 5-hour", "used": 8.4, "limit": 100, "unit": "%", "resets_at": "2026-11-24T18:57:43Z"},
        {"label": "Gemini models", "used": 26.0, "limit": 100, "unit": "%", "resets_at": "2026-12-01T13:19:43Z"},
    ]


def test_a_summary_gemini_group_wins_over_per_model_buckets():
    summary = {"groups": [{"displayName": "Gemini models", "buckets": [{"window": "weekly", "remainingFraction": 0.5}]}]}
    assert [w["label"] for w in quota["parse"](summary, PER_MODEL, NOW)["windows"]] == ["Gemini models · weekly"]


def test_unreadable_fractions_are_left_out_not_zero():
    summary = {"groups": [{"displayName": "X", "buckets": [{"window": "5h"}, {"window": "weekly", "remainingFraction": "1"},
                                                           {"window": "weekly", "remainingFraction": 2}]}]}
    assert quota["parse"](summary, {"buckets": [{"modelId": "gemini-x", "remainingFraction": 0.5}]}, NOW)["windows"] == []


def test_token_file_is_read_and_expiry_respected(tmp_path):
    path = tmp_path / "antigravity-oauth-token"
    assert quota["read_token"](str(path)) is None
    path.write_text(json.dumps({"token": {"access_token": "tok", "expiry": "2026-11-24T14:19:22.993603428Z"}}))
    assert quota["read_token"](str(path), now=NOW - 86400) == ("tok", False)
    assert quota["read_token"](str(path), now=NOW + 86400) == ("tok", True)
    path.write_text(json.dumps({"token": {"refresh_token": "only"}}))
    assert quota["read_token"](str(path)) is None


def test_no_login_is_unknown_and_expired_is_an_error_without_a_call(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("HOME", str(tmp_path))
    assert quota["agy_quota"](NOW, call=lambda *a: calls.append(a)) is None
    assert quota["agy_quota"](NOW, login=("tok", True), call=lambda *a: calls.append(a)) == {
        "error": "agy access token expired; agy renews it on its next run"}
    assert calls == []


def test_vendor_failures_are_sanitized():
    def refuse(token, method):
        raise urllib.error.HTTPError("https://x", 403, "secret vendor text tok", {}, None)
    assert quota["agy_quota"](NOW, login=("tok", False), call=refuse) == {"error": "retrieveUserQuotaSummary HTTP 403"}

    def broken(token, method):
        raise ValueError("tok leaked")
    result = quota["agy_quota"](NOW, login=("tok", False), call=broken)
    assert result == {"error": "retrieveUserQuotaSummary: ValueError"} and "tok" not in json.dumps(result)


def test_per_model_failure_keeps_the_summary():
    def call(token, method):
        if method == "retrieveUserQuota":
            raise OSError("down")
        return SUMMARY
    windows = quota["agy_quota"](NOW, login=("tok", False), call=call)["windows"]
    assert [w["label"] for w in windows] == ["Claude and GPT models · weekly", "Claude and GPT models · 5-hour"]
