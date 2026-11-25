"""The dashboard served over real local HTTP against a temporary accounts file: save, reload, refuse."""

import gzip
import io
import json
import os
import runpy
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ui = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_ui.py"), run_name="account_ui")
CA = "/etc/ssl/certs/ca-certificates.crt"
DEFAULTS = {"chains": {"claude": ["accounts", "codex"], "codex": ["codex", "accounts"]},
            "tiers": [{"name": "opus-sol", "match": ["opus", "sol"],
                       "models": {"anthropic": "claude-opus-5-5", "codex": "gpt-6.1-sol", "cursor": "cursor:claude-opus-5"}}]}


class Router:
    """Stands in for the router's /v1/models; ROUTER_MODELS_JSON points at a real router's answer."""

    def open(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        if url.endswith("/v1/models"):
            path = os.environ.get("ROUTER_MODELS_JSON")
            return io.BytesIO(Path(path).read_bytes() if path else json.dumps(
                {"data": [{"display_name": "gpt-6.1-sol (codex)"}], "fallback_defaults": DEFAULTS}).encode())
        raise urllib.error.URLError("no usage here")


@pytest.fixture
def app(tmp_path):
    accounts = tmp_path / "accounts.json"
    accounts.write_text(json.dumps({"main": "a", "accounts": {"a": 1}, "renamed": {"old": "a"}}))
    board = ui["Dashboard"](accounts, "http://127.0.0.1:9", Path(CA))
    board.opener = Router()
    board.provider_quota = dict
    server = ThreadingHTTPServer(("127.0.0.1", 0), ui["handler"](board))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", accounts
    server.shutdown()


def call(base, path, body=None):
    request = urllib.request.Request(base + path, data=body and json.dumps(body).encode())
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


@pytest.mark.parametrize("header, expected", [("gzip", True), ("br, gzip;q=0.5", True),
                                              ("gzip;q=0", False), ("x-gzip", False), ("gzip;q=bad", False)])
def test_gzip_negotiation_respects_coding_and_quality(header, expected):
    assert ui["accepts_gzip"](header) is expected


def test_document_compression_preserves_the_page_and_reports_server_time(app):
    base, _ = app
    _, _, plain = call(base, "/")
    request = urllib.request.Request(base + "/", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request) as response:
        compressed = response.read()
        assert response.headers["Content-Encoding"] == "gzip"
        assert response.headers["Vary"] == "Accept-Encoding"
        assert response.headers["Server-Timing"].startswith("app;dur=")
    assert gzip.decompress(compressed) == plain
    assert len(compressed) < len(plain) / 2


def test_tiers_save_to_the_file_and_reload_through_state(app):
    base, accounts = app
    _, _, raw = call(base, "/api/state")
    assert json.loads(raw)["fallback_defaults"] == DEFAULTS
    tiers = [{"name": "mine", "match": ["opus"], "models": {"anthropic": "claude-opus-5-5", "codex": "sol[1m]"}}]
    status, _, raw = call(base, "/api/config", {"fallback": {"tiers": tiers}})
    assert status == 200 and json.loads(raw)["fallback"]["tiers"] == tiers
    assert json.loads(accounts.read_text())["fallback"]["tiers"] == tiers
    assert json.loads(accounts.read_text())["renamed"] == {"old": "a"}
    assert json.loads(call(base, "/api/state")[2])["fallback"]["tiers"] == tiers, "a reload shows what was saved"
    status, _, raw = call(base, "/api/config", {"fallback": {"tiers": [{"name": "x", "match": [], "models": {}}]}})
    assert status == 400 and "match keys" in json.loads(raw)["error"]
    assert json.loads(accounts.read_text())["fallback"]["tiers"] == tiers, "a refused save changes nothing"
    call(base, "/api/config", {"fallback": {"tiers": []}})
    assert "tiers" not in json.loads(accounts.read_text()).get("fallback", {})


def test_console_priority_roundtrips_without_secrets_or_policy_changes(app, capsys):
    base, accounts = app
    cfg = json.loads(accounts.read_text())
    member = {"key": "sk-ant-api03-fake-test-key", "preferred": False, "monthly_budget_usd": 180}
    cfg.update(api_key_accounts={"console": member}, disabled=["console"],
               fallback={"chains": {"claude-fable*": ["accounts"], "fable": ["accounts"]}})
    accounts.write_text(json.dumps(cfg))
    for preferred in (True, False):
        status, _, raw = call(base, "/api/config", {"api_key_accounts": {"console": {"preferred": preferred}}})
        assert status == 200
        for response in (raw, call(base, "/api/state")[2]):
            card = next(a for a in json.loads(response)["accounts"] if a["alias"] == "console")
            assert card["preferred"] is preferred and card["disabled"] is True
            assert card["budget_usd"] == 180
            assert member["key"] not in response.decode()
        expected = {**cfg, "api_key_accounts": {"console": {**member, "preferred": preferred}}}
        assert json.loads(accounts.read_text()) == expected
    assert member["key"] not in capsys.readouterr().err


@pytest.mark.parametrize("patch", [None, [], {"unknown": {"preferred": True}},
    {"console": True}, {"console": {"preferred": "true"}}, {"console": {"preferred": 1}},
    {"console": {"preferred": None}}, {"console": {}},
    {"console": {"preferred": True, "monthly_budget_usd": 999}},
    {"console": {"key": "secret"}},
    {"console": {"preferred": True}, "unknown": {"preferred": False}}])
def test_console_priority_rejects_invalid_patches_atomically(app, patch):
    base, accounts = app
    cfg = json.loads(accounts.read_text())
    cfg["api_key_accounts"] = {"console": {"key": "sk-ant-api03-fake-test-key", "monthly_budget_usd": 180}}
    accounts.write_text(json.dumps(cfg))
    before = accounts.read_bytes()
    status, _, raw = call(base, "/api/config", {"api_key_accounts": patch})
    assert status == 400
    assert b"fake-test-key" not in raw
    assert accounts.read_bytes() == before


def test_only_the_whitelisted_stylesheet_is_served(app):
    base, _ = app
    status, headers, body = call(base, "/pico.css")
    assert status == 200 and headers["content-type"].startswith("text/css") and b"MIT" in body[:200]
    assert call(base, "/pico.conditional.min.css")[0] == 404
    assert call(base, "/../account_ui.py")[0] == 404


def test_insights_without_a_traffic_database_say_so_instead_of_zeros(app):
    base, _ = app
    status, _, raw = call(base, "/api/insights?hours=24")
    assert status == 200 and json.loads(raw) == {"hours": 24, "traffic_db": False}
