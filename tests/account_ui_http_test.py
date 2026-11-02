"""The dashboard served over real local HTTP against a temporary accounts file: save, reload, refuse."""

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


def test_only_the_whitelisted_stylesheet_is_served(app):
    base, _ = app
    status, headers, body = call(base, "/pico.css")
    assert status == 200 and headers["content-type"].startswith("text/css") and b"MIT" in body[:200]
    assert call(base, "/pico.conditional.min.css")[0] == 404
    assert call(base, "/../account_ui.py")[0] == 404
