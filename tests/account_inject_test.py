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
