"""Only newly registered native sessions cross the HQ routing boundary."""

import importlib.util
import json
from pathlib import Path

import pytest
from mitmproxy import http
from mitmproxy.test import tflow

spec = importlib.util.spec_from_file_location(
    "remote_route", Path(__file__).parents[1] / "scripts/account_remote_route.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture
def route():
    addon = module.NativeRemoteRoute()
    addon.origin = "http://127.0.0.1:9000"
    addon.key = "toy-hq-secret"
    return addon


def flow(route, path, payload=None, method="POST", token="real-oauth-toy"):
    value = tflow.tflow()
    value.request = http.Request.make(
        method,
        "https://api.anthropic.com" + path,
        json.dumps(payload or {}).encode(),
        {
            "Authorization": "Bearer " + token,
            "Cookie": "account-cookie",
            "x-api-key": "api-key",
        },
    )
    route.requestheaders(value)
    return value


def test_registration_routes_after_map_remote_and_strips_account_secrets(route):
    value = flow(route, "/v1/code/sessions", {"bridge": {}, "title": "Toy"})
    value.request.host = "127.0.0.1"  # Existing map_remote hook may run first.
    route.request(value)
    assert value.request.url == route.origin + "/v1/code/sessions"
    assert value.request.headers["x-hq-native-key"] == route.key
    assert not {"authorization", "cookie", "x-api-key"} & set(value.request.headers)
    value.response = http.Response.make(200, b'{"session":{"id":"cse_toy"}}')
    route.responseheaders(value)
    assert value.response.stream is False
    route.response(value)
    assert route.sessions == {"cse_toy": None}


def test_worker_auth_uses_only_issued_token(route):
    route.sessions["cse_toy"] = None
    bridge = flow(route, "/v1/code/sessions/cse_toy/bridge")
    route.request(bridge)
    bridge.response = http.Response.make(200, b'{"worker_jwt":"worker-secret"}')
    route.response(bridge)
    worker = flow(
        route,
        "/v1/code/sessions/cse_toy/worker/events/stream?from_sequence_num=7",
        method="GET",
        token="worker-secret",
    )
    route.request(worker)
    assert worker.request.url.endswith("/worker/events/stream?from_sequence_num=7")
    assert worker.request.headers["authorization"] == "Bearer worker-secret"
    assert "x-hq-native-key" not in worker.request.headers
    worker.response = http.Response.make(200)
    worker.response.stream = True
    route.responseheaders(worker)
    assert worker.response.stream is True
    wrong = flow(route, "/v1/code/sessions/cse_toy/worker")
    route.request(wrong)
    assert wrong.response.status_code == 401
    assert "authorization" not in wrong.request.headers


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/v1/messages", {"model": "gpt-toy"}),
        ("/v1/code/sessions", {"title": "ordinary cloud session"}),
        ("/v1/code/sessions/cse_not_ours/bridge", {}),
        ("/api/oauth/usage", {}),
    ],
)
def test_unrelated_account_requests_unchanged(route, path, payload):
    value = flow(route, path, payload)
    before = value.request.url
    route.request(value)
    assert value.request.url == before
    assert value.request.headers["authorization"] == "Bearer real-oauth-toy"


def test_host_lookalike_cannot_use_registration_key(route):
    value = tflow.tflow()
    value.request = http.Request.make(
        "POST", "https://api.anthropic.com.evil.test/v1/code/sessions", b'{"bridge":{}}'
    )
    route.requestheaders(value)
    route.request(value)
    assert "x-hq-native-key" not in value.request.headers
