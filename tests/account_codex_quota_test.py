import http.client
import json
import runpy
import urllib.error
from pathlib import Path

import pytest

quota = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_codex_quota.py"), run_name="account_codex_quota")
NOW = 1_793_000_000
USAGE = {
    "plan_type": "prolite",
    "rate_limit": {"allowed": True, "limit_reached": False,
                   "primary_window": {"used_percent": 6, "limit_window_seconds": 604800, "reset_at": 1793774155},
                   "secondary_window": {"used_percent": 41.5, "limit_window_seconds": 18000, "reset_at": 1793010000}},
    "code_review_rate_limit": None,
    "additional_rate_limits": [{"limit_name": "Spark", "rate_limit": {
        "primary_window": {"used_percent": 2, "limit_window_seconds": 18000, "reset_at": None}}}],
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
}


def test_reply_becomes_percent_windows_with_resets():
    q = quota["codex_quota"](NOW, login=("toy", "acct"), call=lambda login: USAGE)
    assert q["at"] == NOW and "prolite plan" in q["note"]
    assert q["windows"] == [
        {"label": "Weekly", "used": 6.0, "limit": 100, "unit": "%", "resets_at": "2026-11-04T06:35:55+00:00"},
        {"label": "5h", "used": 41.5, "limit": 100, "unit": "%", "resets_at": "2026-10-26T10:20:00+00:00"},
        {"label": "Spark 5h", "used": 2.0, "limit": 100, "unit": "%", "resets_at": None},
    ]


def test_limit_reached_and_credits_are_noted():
    usage = {**USAGE, "rate_limit": {**USAGE["rate_limit"], "limit_reached": True},
             "credits": {"has_credits": True, "balance": "12"}}
    note = quota["codex_quota"](NOW, login=("toy", ""), call=lambda login: usage)["note"]
    assert "limit reached" in note and "credit balance 12" in note


def test_no_login_is_unknown(tmp_path):
    assert quota["read_login"](str(tmp_path / "missing.json")) is None
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "  "}}))
    assert quota["read_login"](str(tmp_path / "auth.json")) is None
    assert quota["codex_quota"](NOW, login=False) is None


def test_login_is_read_trimmed_without_changing_the_file(tmp_path):
    path = tmp_path / "auth.json"
    original = json.dumps({"tokens": {"access_token": " toy-private-token\n", "account_id": "acct"}})
    path.write_text(original)
    assert quota["read_login"](str(path)) == ("toy-private-token", "acct")
    assert path.read_text() == original


def test_http_error_is_a_status_not_a_meter():
    def call(login):
        raise urllib.error.HTTPError("u", 401, "no", {}, None)

    assert quota["codex_quota"](NOW, login=("toy", ""), call=call) == {"error": "Codex usage HTTP 401"}


@pytest.mark.parametrize("failure", [
    http.client.IncompleteRead(b"truncated"),
    ValueError("Invalid header value b'Bearer toy-private-token\\n'"),
])
def test_failures_never_echo_credentials(failure):
    def call(login):
        raise failure

    result = quota["codex_quota"](NOW, login=("toy-private-token", ""), call=call)
    assert result == {"error": f"Codex usage: {type(failure).__name__}"}
    assert "toy-private-token" not in json.dumps(result)


@pytest.mark.parametrize("value", [[], {"rate_limit": {"primary_window": {"used_percent": "lots"}}}])
def test_malformed_reply_is_an_error(value):
    assert "error" in quota["codex_quota"](NOW, login=("toy", ""), call=lambda login: value)
