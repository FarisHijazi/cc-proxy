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


def test_restarted_proxy_keeps_routing_known_sessions(tmp_path):
    from mitmproxy.test import taddons

    key = tmp_path / "key"
    key.write_text("k" * 40)
    options = dict(
        hq_remote_origin="http://127.0.0.1:9000",
        hq_remote_key_file=str(key),
        hq_remote_sessions_file=str(tmp_path / "sessions.json"),
    )
    before = module.NativeRemoteRoute()
    with taddons.context(before) as tctx:
        tctx.configure(before, **options)
        create = flow(before, "/v1/code/sessions", {"bridge": {}, "title": "Toy"})
        before.request(create)
        create.response = http.Response.make(200, b'{"session":{"id":"cse_toy"}}')
        before.response(create)
        bridge = flow(before, "/v1/code/sessions/cse_toy/bridge")
        before.request(bridge)
        bridge.response = http.Response.make(200, b'{"worker_jwt":"worker-secret"}')
        before.response(bridge)
    assert (tmp_path / "sessions.json").stat().st_mode & 0o777 == 0o600
    after = module.NativeRemoteRoute()
    with taddons.context(after) as tctx:
        tctx.configure(after, **options)
        worker = flow(after, "/v1/code/sessions/cse_toy/worker/events", token="worker-secret")
        after.request(worker)
    assert worker.request.url == "http://127.0.0.1:9000/v1/code/sessions/cse_toy/worker/events"
    assert worker.request.headers["authorization"] == "Bearer worker-secret"


inject_spec = importlib.util.spec_from_file_location(
    "account_inject", Path(__file__).parents[1] / "scripts/account_inject.py"
)
inject_module = importlib.util.module_from_spec(inject_spec)
inject_spec.loader.exec_module(inject_module)


def served(route, monkeypatch, placeholder, path="/v1/code/sessions", payload=None, headers=()):
    """One request through the --serve proxy's addon chain, inject loaded first."""
    monkeypatch.setattr(inject_module, "slot_token", lambda slot: (f"real-{slot}", 4102444800000))
    inject = inject_module.AccountInject()
    inject.accounts = {"personal": "1", "work": "2"}
    value = tflow.tflow()
    value.request = http.Request.make(
        "POST",
        "https://api.anthropic.com" + path,
        json.dumps({"bridge": {}, "title": "Toy"} if payload is None else payload).encode(),
        {"Authorization": "Bearer proxy-account-" + placeholder, **dict(headers)},
    )
    inject.requestheaders(value)
    route.requestheaders(value)
    route.request(value)
    return value


def test_private_registration_is_marked_for_hq(route, monkeypatch):
    for placeholder in ("work+rc=personal+private", "work+private"):
        value = served(route, monkeypatch, placeholder)
        assert value.request.url == route.origin + "/v1/code/sessions"
        assert value.request.headers["x-hq-private"] == "1"
        assert "authorization" not in value.request.headers
    value = served(route, monkeypatch, "work+rc=personal")
    assert "x-hq-private" not in value.request.headers
    forged = served(route, monkeypatch, "work", headers={"x-hq-private": "1"})
    assert "x-hq-private" not in forged.request.headers


def test_private_session_the_route_does_not_claim_reaches_anthropic_without_a_token(route, monkeypatch):
    for payload in ({"title": "Toy"}, {"bridge": {}}):
        value = served(route, monkeypatch, "work+rc=personal+private", payload=payload)
        claimed = value.request.url.startswith(route.origin)
        assert claimed == ("bridge" in payload)
        assert "authorization" not in value.request.headers


def test_mirror_requests_reach_anthropic_with_the_injected_token(route, monkeypatch):
    route.sessions["cse_hq"] = "worker-secret"
    for path in ("/v1/code/sessions", "/v1/code/sessions/cse_hq/bridge"):
        value = served(route, monkeypatch, "personal", path, headers={"x-hq-mirror": "1"})
        assert value.request.url == "https://api.anthropic.com" + path
        assert value.request.headers["authorization"] == "Bearer real-1"
        assert not {"x-hq-mirror", "x-hq-native-key"} & set(value.request.headers)
        assert "hq_remote" not in value.metadata
    routed = served(route, monkeypatch, "personal", headers={"x-hq-mirror": "0"})
    assert routed.request.url == route.origin + "/v1/code/sessions"
    assert "x-hq-mirror" not in routed.request.headers
