"""Native bridge selection: identity/auth rewrite with opaque action transport."""

import importlib.util
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from mitmproxy import http, websocket
from mitmproxy.test import tflow

spec = importlib.util.spec_from_file_location(
    "chrome_route", Path(__file__).parents[1] / "scripts/account_chrome_route.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def snapshot(tmp_path, name):
    directory = tmp_path / name
    directory.mkdir()
    identity = str(uuid4())
    (directory / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": identity}})
    )
    (directory / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": name + "-access",
                    "scopes": ["user:profile"],
                    "expiresAt": (time.time() + 600) * 1000,
                }
            }
        )
    )
    return {
        "config_dir": str(directory),
        "secure_storage_dir": str(directory),
    }, identity


def configured(tmp_path):
    route = module.NativeChromeRoute()
    route.source, route.source_id = snapshot(tmp_path, "main")
    route.target, route.target_id = snapshot(tmp_path, "chrome")
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET",
        f"https://bridge.claudeusercontent.com/chrome/{route.source_id}",
        headers={"Upgrade": "websocket"},
    )
    flow.websocket = websocket.WebSocketData()
    return route, flow


def message(flow, payload, client=True):
    content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    item = websocket.WebSocketMessage(1, client, content)
    flow.websocket.messages.append(item)
    return item


def test_changes_only_bridge_identity_and_first_connect(tmp_path):
    route, flow = configured(tmp_path)
    route.request(flow)
    assert flow.request.path == f"/chrome/{route.target_id}"
    auth = message(
        flow,
        {
            "type": "connect",
            "client_type": "claude-code",
            "oauth_token": "main-access",
            "other": "preserved",
        },
    )
    route.websocket_message(flow)
    assert json.loads(auth.content) == {
        "type": "connect",
        "client_type": "claude-code",
        "oauth_token": "chrome-access",
        "other": "preserved",
    }
    for payload, client in (
        (b"opaque binary-like action", True),
        (b'{"type":"permission_request"}', False),
    ):
        item = message(flow, payload, client)
        route.websocket_message(flow)
        assert item.content == payload and not item.dropped


@pytest.mark.parametrize(
    "url",
    [
        "https://api.anthropic.com/chrome/{id}",
        "https://bridge.claudeusercontent.com.evil.test/chrome/{id}",
        "https://bridge.claudeusercontent.com/devices/{id}/bridge",
        "https://bridge.claudeusercontent.com/chrome/{id}?query=1",
        "http://bridge.claudeusercontent.com/chrome/{id}",
        "https://bridge.claudeusercontent.com:444/chrome/{id}",
    ],
)
def test_unrelated_endpoints_are_unchanged(tmp_path, url):
    route, flow = configured(tmp_path)
    flow.request = http.Request.make(
        "GET", url.format(id=route.source_id), headers={"Upgrade": "websocket"}
    )
    before = flow.request.url
    route.request(flow)
    assert flow.request.url == before and not flow.metadata.get("native_chrome_route")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "type": "connect",
            "client_type": "claude-code",
            "oauth_token": "another-account",
        },
        {"type": "connect", "client_type": "extension", "oauth_token": "main-access"},
        {"type": "tool_call"},
        [],
        b"not json",
    ],
)
def test_wrong_identity_or_protocol_never_forwards(tmp_path, payload):
    route, flow = configured(tmp_path)
    route.request(flow)
    item = message(flow, payload)
    flow.live = True
    route.websocket_message(flow)
    assert item.dropped and flow.error


def test_expired_target_fails_without_main_fallback(tmp_path):
    route, flow = configured(tmp_path)
    route.request(flow)
    path = Path(route.target["secure_storage_dir"]) / ".credentials.json"
    data = json.loads(path.read_text())
    data["claudeAiOauth"]["expiresAt"] = 1
    path.write_text(json.dumps(data))
    item = message(
        flow,
        {"type": "connect", "client_type": "claude-code", "oauth_token": "main-access"},
    )
    flow.live = True
    route.websocket_message(flow)
    assert item.dropped and flow.error
