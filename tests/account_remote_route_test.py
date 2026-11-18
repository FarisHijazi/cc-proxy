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
    options = {
        "hq_remote_origin": "http://127.0.0.1:9000",
        "hq_remote_key_file": str(key),
        "hq_remote_sessions_file": str(tmp_path / "sessions.json"),
    }
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


def mod_flow(route, *, path=module.EVENT_PATH, method="POST", payload=None, mode="regular"):
    from mitmproxy.proxy.mode_specs import ProxyMode

    value = tflow.tflow()
    value.client_conn.proxy_mode = ProxyMode.parse(mode)
    value.request = http.Request.make(method, "https://" + module.EVENT_HOST + path,
        json.dumps(payload if payload is not None else {"native_session_id": "cse_toy", "events": []}),
        {"Authorization": "Bearer toy-secret", "Cookie": "toy-cookie", "x-api-key": "ccp-toy",
         "proxy-authorization": "toy-proxy", "x-hq-native-key": "forged", "x-hq-mirror": "1",
         "x-hq-private": "1", "x-claude-codex-pool": "toy-pool"})
    route.requestheaders(value)
    return value


def test_mod_batches_use_existing_owner_without_forwarding_credentials(route, tmp_path):
    route.sessions["cse_toy"] = "issued-worker"
    route.store = str(tmp_path / "sessions.json")
    value = mod_flow(route)
    original = value.request.content
    route.request(value)
    assert value.request.url == route.origin + module.EVENT_PATH
    assert value.request.content == original
    assert dict(value.request.headers) == {
        "content-type": "application/json", "content-length": str(len(original)),
        "x-hq-native-key": route.key, "host": "127.0.0.1:9000",
    }
    value.response = http.Response.make(200, b'{"accepted":0}')
    route.response(value)
    assert route.sessions == {"cse_toy": "issued-worker"}
    assert not Path(route.store).exists()  # Observation never rewrites ownership.


@pytest.mark.parametrize("mode", ["reverse:https://api.anthropic.com", "upstream:http://127.0.0.1:19000"])
def test_mod_batches_cannot_bypass_reverse_api_or_other_proxy_modes(route, mode):
    route.sessions["cse_toy"] = "issued-worker"
    value = mod_flow(route, mode=mode)
    before = value.request.url
    route.request(value)
    assert value.response.status_code == 403
    assert value.request.url == before
    assert value.request.headers["x-hq-native-key"] != route.key


@pytest.mark.parametrize("payload", [{}, {"native_session_id": "cse_other"}, {"native_session_id": []}, []])
def test_mod_batches_cannot_register_or_claim_unknown_owner(route, payload):
    value = mod_flow(route, payload=payload)
    route.request(value)
    assert value.response.status_code == 404
    assert not route.sessions
    assert value.request.host == module.EVENT_HOST


@pytest.mark.parametrize("path,method,status", [
    ("/v1/code/sessions", "POST", 404), (module.EVENT_PATH + "?x=1", "POST", 404),
    (module.EVENT_PATH + "/", "POST", 404), (module.EVENT_PATH, "GET", 405),
])
def test_reserved_host_has_no_other_forwarding_path(route, path, method, status):
    value = mod_flow(route, path=path, method=method)
    route.request(value)
    assert value.response.status_code == status
    assert value.request.host == module.EVENT_HOST


def test_mod_route_preserves_prior_auth_rejection(route):
    route.sessions["cse_toy"] = "issued-worker"
    value = mod_flow(route)
    rejection = http.Response.make(401, b"invalid API key")
    value.response = rejection
    route.request(value)
    assert value.response is rejection
    assert value.request.host == module.EVENT_HOST


def test_disabled_native_route_refuses_events_locally_but_preserves_account_requests():
    from mitmproxy.test import taddons

    route = module.NativeRemoteRoute()
    with taddons.context(route) as context:
        context.configure(route, hq_remote_origin="", hq_remote_key_file="")
    value = mod_flow(route)
    route.request(value)
    assert value.response.status_code == 503
    assert value.request.host == module.EVENT_HOST
    registration = flow(route, "/v1/code/sessions", {"bridge": {}})
    route.request(registration)
    assert registration.response is None
    assert registration.request.host == "api.anthropic.com"


@pytest.mark.parametrize("content,status", [(b"{", 400), (b"x" * (module.MAX_EVENT_BODY + 1), 413)])
def test_mod_invalid_body_is_refused_before_forwarding(route, content, status):
    value = mod_flow(route)
    value.request.content = content
    route.request(value)
    assert value.response.status_code == status
    assert value.request.host == module.EVENT_HOST


@pytest.mark.parametrize("patterns", [[], [r"^api\.anthropic\.com:443$"], [r"^.*:443$"]])
def test_hot_reload_extends_only_restricted_interception_without_duplicates(route, patterns):
    from mitmproxy.test import taddons

    with taddons.context(route) as context:
        context.configure(route, allow_hosts=patterns)
        route.running()
        expected = [*patterns, module.EVENT_HOST_PATTERN] if patterns == [r"^api\.anthropic\.com:443$"] else patterns
        assert context.options.allow_hosts == expected
        route.running()
        assert context.options.allow_hosts == expected


def test_mod_forward_proxy_http1_supplies_trusted_hq_host(tmp_path):
    import shutil
    import socket
    import ssl
    import subprocess
    import threading
    import time
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    executable = shutil.which("mitmdump")
    if not executable:
        pytest.skip("mitmproxy required for the actual HTTP proxy transport")
    seen = []

    class HQ(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.send_response(200 if self.headers.get("host") == f"127.0.0.1:{self.server.server_port}" else 400)
            self.end_headers()
            self.wfile.write(b'{"accepted":1}')

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), HQ)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    key = tmp_path / "key"
    key.write_text("toy-native-key-" + "x" * 40)
    key.chmod(0o600)
    owners = tmp_path / "sessions.json"
    owners.write_text('{"cse_toy":"toy-worker"}')
    ca = tmp_path / "ca"
    command = [executable, "--listen-host", "127.0.0.1", "--listen-port", str(port),
               "--set", f"confdir={ca}", "--set", "connection_strategy=lazy",
               "--set", "upstream_cert=false", "--allow-hosts", r"^api\.anthropic\.com:443$",
               "--scripts", str(Path(__file__).parents[1] / "scripts/account_remote_route.py"),
               "--set", f"hq_remote_origin=http://127.0.0.1:{server.server_port}",
               "--set", f"hq_remote_key_file={key}", "--set", f"hq_remote_sessions_file={owners}"]
    with (tmp_path / "mitm.log").open("w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("isolated proxy did not start")
                if (ca / "mitmproxy-ca-cert.pem").exists():
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        pass
                time.sleep(0.05)
            client = urllib.request.build_opener(
                urllib.request.ProxyHandler({"https": f"http://127.0.0.1:{port}"}),
                urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca / "mitmproxy-ca-cert.pem")),
            )
            body = b'{"native_session_id":"cse_toy","events":[]}'
            request = urllib.request.Request("https://" + module.EVENT_HOST + module.EVENT_PATH,
                                             data=body, headers={"Authorization": "Bearer toy-caller"})
            with client.open(request, timeout=5) as response:
                assert response.status == 200
            assert len(seen) == 1
            assert seen[0]["body"] == body and seen[0]["path"] == module.EVENT_PATH
            headers = {key.lower(): value for key, value in seen[0]["headers"].items()}
            assert headers["host"] == f"127.0.0.1:{server.server_port}"
            assert headers["x-hq-native-key"] == key.read_text()
            assert "authorization" not in headers
            assert owners.read_text() == '{"cse_toy":"toy-worker"}'
        finally:
            process.terminate()
            process.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
