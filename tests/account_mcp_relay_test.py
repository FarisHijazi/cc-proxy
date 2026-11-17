"""The public MCP entry uses one existing upstream and preserves authentication and streaming."""

import http.client
import io
import json
import runpy
import threading
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

ui = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_ui.py"), run_name="account_ui")
relay_globals = ui["MCPRelay"].handle.__globals__


@contextmanager
def serving(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def request(origin, path="/mcp", method="POST", body=b"{}", headers=None):
    connection = http.client.HTTPConnection(origin.removeprefix("http://"), timeout=3)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result


@pytest.fixture
def upstream():
    received = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append((self.command, self.path, dict(self.headers), body))
            authenticated = self.headers.get("Authorization") == "Bearer toy-token"
            self.send_response(200 if authenticated else 401)
            self.send_header("Content-Type", "application/json")
            self.send_header("WWW-Authenticate", 'Bearer resource_metadata="https://proxy.example.com/.well-known/oauth-protected-resource/mcp"')
            self.send_header("Mcp-Session-Id", "session-1")
            self.send_header("Set-Cookie", "must-not-leak=1")
            self.end_headers()
            self.wfile.write(b'{"result": "ok"}' if authenticated else b'{"error": "invalid_token"}')

        do_GET = do_DELETE = do_OPTIONS = do_POST

        def log_message(self, *_):
            pass

    with serving(Upstream) as url:
        yield url, received


def test_auth_and_protocol_headers_reach_only_the_fixed_upstream(upstream):
    origin, received = upstream
    relay = ui["MCPRelay"](origin)
    with serving(ui["handler"](None, relay)) as public:
        status, headers, body = request(public)
        assert status == 401 and json.loads(body)["error"] == "invalid_token"
        assert "proxy.example.com" in headers["WWW-Authenticate"]
        status, headers, body = request(public, headers={
            "Authorization": "Bearer toy-token", "Mcp-Protocol-Version": "2025-03-26",
            "Mcp-Session-Id": "session-1", "Accept": "application/json, text/event-stream",
            "Cookie": "dashboard-secret", "X-Forwarded-Host": "evil.example.com",
        })
    assert status == 200 and json.loads(body) == {"result": "ok"}
    assert headers["Mcp-Session-Id"] == "session-1" and "Set-Cookie" not in headers
    forwarded = {k.lower(): v for k, v in received[-1][2].items()}
    assert forwarded["authorization"] == "Bearer toy-token"
    assert forwarded["mcp-protocol-version"] == "2025-03-26"
    assert forwarded["mcp-session-id"] == "session-1"
    assert "cookie" not in forwarded and "x-forwarded-host" not in forwarded
    assert forwarded["host"] == origin.removeprefix("http://")


@pytest.mark.parametrize("method,path", [
    ("GET", "/.well-known/oauth-protected-resource/mcp"),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/mcp"), ("DELETE", "/mcp"), ("OPTIONS", "/mcp/"),
])
def test_mcp_transport_and_discovery_methods_are_forwarded(upstream, method, path):
    origin, received = upstream
    with serving(ui["handler"](None, ui["MCPRelay"](origin))) as public:
        request(public, path, method, headers={"Authorization": "Bearer toy-token"})
    assert received[0][:2] == (method, path)


def test_dashboard_stays_local_and_unrecognized_paths_never_reach_mcp(upstream):
    origin, received = upstream
    dashboard = SimpleNamespace(state=lambda: {"dashboard": True})
    with serving(ui["handler"](dashboard, ui["MCPRelay"](origin))) as public:
        assert json.loads(request(public, "/api/state", "GET")[2]) == {"dashboard": True}
        assert request(public, "/mcp/anything")[0] == 404
        assert request(public, "/.well-known/oauth-protected-resource/anything", "GET")[0] == 404
        assert request(public, "/mcp?upstream=https://evil.example.com")[0] == 400
        assert request(public, "http://evil.example.com/mcp")[0] == 400
    assert received == []


def test_oauth_discovery_redirect_is_returned_without_following_it():
    paths = []

    class Discovery(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(307)
            self.send_header("Location", "https://auth.example.com/application/o/fleet/.well-known/openid-configuration")
            self.end_headers()

        def log_message(self, *_):
            pass

    with serving(Discovery) as origin, serving(ui["handler"](None, ui["MCPRelay"](origin))) as public:
        status, headers, _ = request(public, "/.well-known/oauth-authorization-server", "GET")
    assert status == 307
    assert headers["Location"] == "https://auth.example.com/application/o/fleet/.well-known/openid-configuration"
    assert paths == ["/.well-known/oauth-authorization-server"]


def test_optional_relay_is_off_by_default():
    with serving(ui["handler"](None)) as public:
        assert request(public)[0] == 404


@pytest.mark.parametrize("origin", ["file:///tmp/a", "http://", "https://user:pass@example.com", "https://example.com/mcp", "http://example.com?key=secret", "http://example.com#fragment"])
def test_rejects_ambiguous_upstream_configuration(origin):
    with pytest.raises(ValueError):
        ui["MCPRelay"](origin)


def test_request_limit_is_checked_before_forwarding(upstream):
    origin, received = upstream
    with serving(ui["handler"](None, ui["MCPRelay"](origin))) as public:
        assert request(public, headers={"Content-Length": str(1024 * 1024 + 1)})[0] == 413
    assert received == []


def test_sse_chunk_is_delivered_before_upstream_finishes():
    release = threading.Event()

    class SSE(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"data: first\n\n"
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
            release.wait(3)
            self.wfile.write(b"0\r\n\r\n")

        def log_message(self, *_):
            pass

    with serving(SSE) as origin, serving(ui["handler"](None, ui["MCPRelay"](origin))) as public:
        connection = http.client.HTTPConnection(public.removeprefix("http://"), timeout=1)
        try:
            connection.request("POST", "/mcp", body=b"{}")
            response = connection.getresponse()
            assert response.getheader("Content-Type") == "text/event-stream"
            assert response.read(13) == b"data: first\n\n"
        finally:
            release.set()
            connection.close()


def test_dropped_action_is_never_retried():
    attempts = []

    class Dropped(BaseHTTPRequestHandler):
        def do_POST(self):
            attempts.append(self.path)
            self.connection.close()

        def log_message(self, *_):
            pass

    with serving(Dropped) as origin, serving(ui["handler"](None, ui["MCPRelay"](origin))) as public:
        status, _, body = request(public)
    assert status == 502 and b"Do not retry writes" in body
    assert attempts == ["/mcp"]


def test_body_receive_has_a_total_deadline(monkeypatch):
    elapsed = [0]
    reads, replies = [], []
    headers = Message()
    headers["Content-Length"] = "3"

    def slow_read(_):
        reads.append(True)
        elapsed[0] += 6
        return b"x"

    monkeypatch.setitem(relay_globals, "BODY_TIMEOUT", 10)
    monkeypatch.setitem(relay_globals, "monotonic", lambda: elapsed[0])
    handler = SimpleNamespace(path="/mcp", command="POST", headers=headers,
        connection=SimpleNamespace(settimeout=lambda _: None),
        rfile=SimpleNamespace(read1=slow_read, read=slow_read),
        reply=lambda status, _: replies.append(status))
    assert ui["MCPRelay"]("http://unused.example.com").handle(handler)
    assert replies == [408]
    assert len(reads) == 2


def test_sse_stream_keeps_waiting_across_idle_gaps(monkeypatch):
    socket = SimpleNamespace(timeout=65)
    socket.settimeout = lambda value: setattr(socket, "timeout", value)
    chunks = iter([b"data: first\n\n", b"data: after-idle\n\n", b""])

    def read_chunk(_):
        if socket.timeout is not None:
            raise TimeoutError("simulated idle gap exceeds the socket timeout")
        return next(chunks)

    response = SimpleNamespace(status=200, getheaders=lambda: [("Content-Type", "text/event-stream")],
        getheader=lambda name, default=None: "text/event-stream" if name.lower() == "content-type" else default,
        read1=read_chunk)
    connection = SimpleNamespace(sock=socket, request=lambda *a, **k: None,
        getresponse=lambda: response, close=lambda: None)
    monkeypatch.setattr(http.client, "HTTPConnection", lambda *a, **k: connection)
    output = io.BytesIO()
    handler = SimpleNamespace(path="/mcp", command="GET", headers=Message(),
        connection=SimpleNamespace(settimeout=lambda _: None), rfile=io.BytesIO(), wfile=output,
        send_response=lambda _: None, send_header=lambda *args: None, end_headers=lambda: None,
        reply=lambda *args: pytest.fail("The stream must remain open"))
    assert ui["MCPRelay"]("http://upstream.example.com").handle(handler)
    assert output.getvalue() == b"data: first\n\ndata: after-idle\n\n"


def test_mcp_concurrency_limit_preserves_dashboard_access(monkeypatch):
    release = threading.Event()
    monkeypatch.setitem(relay_globals, "MAX_IN_FLIGHT", 1)

    class SSE(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            release.wait(3)

        def log_message(self, *_):
            pass

    dashboard = SimpleNamespace(state=lambda: {"dashboard": True})
    with serving(SSE) as origin, serving(ui["handler"](dashboard, ui["MCPRelay"](origin))) as public:
        connection = http.client.HTTPConnection(public.removeprefix("http://"), timeout=1)
        try:
            connection.request("GET", "/mcp")
            assert connection.getresponse().read(13) == b"data: first\n\n"
            assert request(public, method="GET")[0] == 503
            assert json.loads(request(public, "/api/state", "GET")[2]) == {"dashboard": True}
        finally:
            release.set()
            connection.close()
