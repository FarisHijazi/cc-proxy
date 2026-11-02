"""The dashboard's settings writes: validated, atomic, and kept to the keys it owns."""

import io
import json
import runpy
from pathlib import Path

import pytest

ui = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_ui.py"), run_name="account_ui")


def dashboard(tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"main": "a", "accounts": {"a": 1, "b": 2}, "renamed": {"old": "a"}}))
    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.accounts_file = accounts
    board.state = lambda: json.loads(accounts.read_text())
    return board, accounts


def test_settings_are_saved_with_a_backup_and_other_keys_kept(tmp_path):
    board, accounts = dashboard(tmp_path)
    saved = board.update({"order": ["b", "a"], "balance": {"strategy": "prefer", "prefer": "b"},
                          "disabled": ["a"], "traffic_days": 30})
    assert list(saved["accounts"]) == ["b", "a"]
    assert saved["balance"] == {"strategy": "prefer", "prefer": "b"}
    assert saved["disabled"] == ["a"] and saved["traffic_days"] == 30
    assert saved["renamed"] == {"old": "a"}
    assert (tmp_path / "accounts.json.before-ui").exists()
    assert oct(accounts.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("change", [
    {"order": ["a"]},
    {"balance": {"strategy": "random"}},
    {"balance": {"strategy": "prefer", "prefer": "nobody"}},
    {"disabled": ["nobody"]},
    {"traffic_days": 0},
    {"fallback": {"threshold": 0}},
    {"fallback": {"threshold": 101}},
    {"fallback": {"threshold": True}},
    {"fallback": {"on": {"timeout": True}}},
    {"fallback": {"on": {"auth": "yes"}}},
    {"fallback": {"chains": {"claude": "codex"}}},
    {"fallback": {"chains": {"claude": ["codex", " "]}}},
])
def test_invalid_settings_are_refused_and_nothing_is_written(tmp_path, change):
    board, accounts = dashboard(tmp_path)
    before = accounts.read_text()
    with pytest.raises(ValueError):
        board.update(change)
    assert accounts.read_text() == before


def test_failover_settings_are_saved_and_cleared(tmp_path):
    board, accounts = dashboard(tmp_path)
    fallback = {"chains": {"claude": ["accounts", " gemini-3-pro "], "gemini": ["gemini", "accounts"]},
                "on": {"rate_limit": True, "auth": True, "server_error": True, "model_unavailable": True}, "threshold": 90}
    saved = board.update({"fallback": fallback})
    assert saved["fallback"]["chains"]["claude"] == ["accounts", "gemini-3-pro"]
    assert saved["fallback"]["threshold"] == 90 and saved["fallback"]["on"]["server_error"] is True
    assert saved["fallback"]["on"]["model_unavailable"] is True
    assert board.update({"fallback": {"on": {"model_unavailable": False}}})["fallback"]["on"]["model_unavailable"] is False
    assert "threshold" not in board.update({"fallback": {**fallback, "threshold": None}})["fallback"]
    assert "fallback" not in board.update({"fallback": None})
    assert list(json.loads(accounts.read_text())["accounts"]) == ["a", "b"]


def test_providers_come_from_the_router_model_list():
    class Opener:
        def open(self, url, timeout):
            assert url == "https://api.anthropic.com/v1/models"
            return io.BytesIO(json.dumps({"data": [
                {"display_name": "claude-opus-5-5 (anthropic)"},
                {"display_name": "gpt-6.1-sol (codex)"},
                {"display_name": "gemini-3-pro (gemini)"},
            ]}).encode())

    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.opener, board.cache = Opener(), {}
    providers = board.providers()
    assert providers["reachable"]
    assert providers["models"]["codex"] == ["gpt-6.1-sol"]
    assert providers["models"]["gemini"] == ["gemini-3-pro"]
    assert providers["models"]["cursor-cli"] == [], "a backend that is not signed in is listed empty"


def test_setup_tokens_are_recognised_and_read_usage_from_reply_headers():
    assert ui["inference_only"]("HTTP Error 401: Unauthorized")
    assert not ui["inference_only"](None)
    windows = ui["reply_usage"]([
        ("anthropic-ratelimit-unified-5h-utilization", "0.25"),
        ("anthropic-ratelimit-unified-5h-reset", "1793121600"),
        ("anthropic-ratelimit-unified-7d-utilization", "0.85"),
    ])
    assert windows["five_hour"]["utilization"] == 25.0
    assert windows["five_hour"]["resets_at"].startswith("2026-")
    assert windows["seven_day"] == {"utilization": 85.0, "resets_at": None}


TIERS = [{"name": "heavy", "match": ["opus", " sol "], "models": {"anthropic": "claude-opus-5-5", "codex": "sol[1m]", "cursor": "cursor:claude-opus-5"}}]


def test_tiers_are_validated_saved_and_kept_across_other_failover_edits(tmp_path):
    board, accounts = dashboard(tmp_path)
    saved = board.update({"fallback": {"tiers": TIERS}})["fallback"]
    assert saved["tiers"][0]["match"] == ["opus", "sol"], "keys are stripped"
    assert saved["tiers"][0]["models"]["codex"] == "sol[1m]", "aliases are accepted as destinations"
    edited = board.update({"fallback": {"on": {"auth": False}, "chains": {"claude": ["accounts"]}}})["fallback"]
    assert edited["tiers"] == saved["tiers"], "a failover edit without tiers keeps them"
    assert "tiers" not in board.update({"fallback": {"tiers": []}})["fallback"], "an empty list means router defaults"
    board.update({"fallback": {"tiers": TIERS}})
    assert "tiers" not in board.update({"fallback": None}).get("fallback", {})
    assert json.loads(accounts.read_text())["renamed"] == {"old": "a"}


def tier(**kw):
    return {"name": "t", "match": ["k"], "models": {"codex": "gpt-x"}, **kw}


@pytest.mark.parametrize("tiers", [
    "opus",
    [tier(), tier(name="T", match=["other"])],
    [tier(), tier(name="u")],
    [tier(match=["a", "A"])],
    [tier(match=[])],
    [tier(match=[" "])],
    [tier(match=["x" * 65])],
    [tier(match=[str(i) for i in range(9)])],
    [tier(models={})],
    [tier(models={"gemini": "gemini-3-pro"})],
    [tier(models={"codex": "bad id"})],
    [tier(models={"codex": "x" * 129})],
    [tier(name="")],
    [{"name": "t", "match": ["k"]}],
    [{**tier(), "extra": 1}],
    [tier(name=f"n{i}", match=[f"k{i}"]) for i in range(25)],
    [tier(name=f"n{i}", match=[f"k{i}" + "x" * 50 for _ in range(1)], models={"anthropic": "a" * 128, "codex": "b" * 128, "cursor": "c" * 128})
     for i in range(24)],
])
def test_invalid_tiers_are_refused_and_nothing_is_written(tmp_path, tiers):
    board, accounts = dashboard(tmp_path)
    before = accounts.read_text()
    with pytest.raises(ValueError):
        board.update({"fallback": {"tiers": tiers}})
    assert accounts.read_text() == before


def test_router_defaults_travel_through_the_provider_read():
    defaults = {"chains": {"claude": ["accounts", "codex"]}, "tiers": TIERS}

    class Opener:
        def open(self, url, timeout):
            return io.BytesIO(json.dumps({"data": [{"display_name": "gpt-6.1-sol (codex)"}], "fallback_defaults": defaults}).encode())

    board = ui["Dashboard"].__new__(ui["Dashboard"])
    board.opener, board.cache = Opener(), {}
    assert board.providers()["fallback_defaults"] == defaults

    class Down:
        def open(self, url, timeout):
            raise OSError("down")

    board.opener, board.cache = Down(), {}
    assert board.providers() == {"reachable": False, "models": {n: [] for n in ui["PROVIDERS"]}, "fallback_defaults": None}


@pytest.mark.parametrize("model", ["opus", "opus[1m]", "sonnet", "claude-opus-5-5[1m]"])
def test_anthropic_equivalence_requires_a_full_upstream_model_id(tmp_path, model):
    board, accounts = dashboard(tmp_path)
    before = accounts.read_text()
    with pytest.raises(ValueError, match="full claude-.*model id"):
        board.update({"fallback": {"tiers": [tier(models={"anthropic": model})]}})
    assert accounts.read_text() == before
