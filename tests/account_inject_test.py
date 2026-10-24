"""Server mode: placeholder logins are swapped for the proxy's own tokens."""

import importlib.util
import json
import time
from pathlib import Path

from mitmproxy import http, websocket
from mitmproxy.test import tflow

spec = importlib.util.spec_from_file_location(
    "account_inject", Path(__file__).parents[1] / "scripts/account_inject.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def addon(monkeypatch, calls):
    def slot_token(slot):
        calls.append(slot)
        return f"real-{slot}", time.time() * 1000 + 3_600_000

    monkeypatch.setattr(module, "slot_token", slot_token)
    inject = module.AccountInject()
    inject.accounts = {"personal": "1"}
    return inject


def request(authorization):
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET",
        "https://api.anthropic.com/api/oauth/profile",
        headers={"Authorization": authorization},
    )
    return flow


def test_placeholder_becomes_the_slot_token_and_is_cached(monkeypatch):
    calls = []
    inject = addon(monkeypatch, calls)
    for _ in range(2):
        flow = request("Bearer proxy-account-personal")
        inject.requestheaders(flow)
        assert flow.request.headers["authorization"] == "Bearer real-1"
    assert calls == ["1"]


def test_real_tokens_pass_through(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = request("Bearer sk-ant-oat01-own")
    inject.requestheaders(flow)
    assert flow.request.headers["authorization"] == "Bearer sk-ant-oat01-own"
    assert flow.response is None


def test_unknown_alias_is_answered_locally(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = request("Bearer proxy-account-other")
    inject.requestheaders(flow)
    assert flow.response.status_code == 401
    assert flow.request.headers["authorization"] == "Bearer proxy-account-other"


def test_bridge_connect_frame_gets_the_slot_token(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = tflow.tflow()
    flow.websocket = websocket.WebSocketData()
    frame = {"type": "connect", "client_type": "claude-code", "oauth_token": "proxy-account-personal"}
    flow.websocket.messages.append(
        websocket.WebSocketMessage(1, True, json.dumps(frame).encode())
    )
    inject.websocket_message(flow)
    assert json.loads(flow.websocket.messages[-1].text)["oauth_token"] == "real-1"


def test_remote_control_owner_applies_only_to_remote_control_paths(monkeypatch):
    calls = []
    inject = addon(monkeypatch, calls)
    inject.accounts = {"personal": "1", "work": "2"}
    for url, expected in (
        ("https://api.anthropic.com/v1/messages", "Bearer real-2"),
        ("https://api.anthropic.com/api/oauth/profile", "Bearer real-2"),
        ("https://api.anthropic.com/v1/code/sessions", "Bearer real-1"),
        ("https://api.anthropic.com/v1/code/sessions/cse_1/worker/events", "Bearer real-1"),
    ):
        flow = tflow.tflow()
        flow.request = http.Request.make(
            "POST", url, headers={"Authorization": "Bearer proxy-account-work+rc=personal"}
        )
        inject.requestheaders(flow)
        assert flow.request.headers["authorization"] == expected, url
    flow = request("Bearer proxy-account-work+rc=nobody")
    inject.requestheaders(flow)
    assert flow.request.headers["authorization"] == "Bearer real-2"
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "POST",
        "https://api.anthropic.com/v1/code/sessions",
        headers={"Authorization": "Bearer proxy-account-work+rc=nobody"},
    )
    inject.requestheaders(flow)
    assert flow.response is not None and flow.response.status_code == 401


def test_private_suffix_is_order_independent_and_never_changes_the_account(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"personal": "1", "work": "2"}
    for placeholder in ("work+rc=personal+private", "work+private+rc=personal"):
        for path, expected in (
            ("/v1/messages", "Bearer real-2"),
            ("/v1/code/sessions", None),
            ("/v1/code/sessions/cse_1/bridge", None),
        ):
            flow = tflow.tflow()
            flow.request = http.Request.make(
                "POST", "https://api.anthropic.com" + path, headers={"Authorization": "Bearer proxy-account-" + placeholder}
            )
            inject.requestheaders(flow)
            assert flow.request.headers.get("authorization") == expected, (placeholder, path)
            assert flow.response is None and flow.metadata["account_private"] is True
    flow = request("Bearer proxy-account-personal")
    inject.requestheaders(flow)
    assert flow.metadata["account_private"] is False


def test_malformed_placeholder_suffix_is_never_forwarded(monkeypatch):
    inject = addon(monkeypatch, [])
    for placeholder in ("personal+shared", "personal+rc=personal+rc=personal"):
        flow = request("Bearer proxy-account-" + placeholder)
        inject.requestheaders(flow)
        assert flow.response.status_code == 401, placeholder
