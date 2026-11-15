"""Expose the existing authenticated fleet MCP on cc-proxy's dashboard listener."""

import http.client
import ssl
from threading import BoundedSemaphore
from time import monotonic
from urllib.parse import urlsplit

PATHS = {"/mcp", "/mcp/", "/.well-known/oauth-protected-resource",
         "/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-authorization-server"}
REQUEST_HEADERS = {"authorization", "accept", "content-type", "mcp-session-id",
                   "mcp-protocol-version", "last-event-id", "origin",
                   "access-control-request-method", "access-control-request-headers"}
RESPONSE_HEADERS = {"content-type", "www-authenticate", "mcp-session-id",
                    "mcp-protocol-version", "allow", "retry-after",
                    "access-control-allow-origin", "access-control-allow-methods",
                    "access-control-allow-headers", "access-control-expose-headers", "vary"}
MAX_BODY = 1024 * 1024
BODY_TIMEOUT = 10
MAX_IN_FLIGHT = 32


class MCPRelay:
    def __init__(self, upstream):
        self.origin = urlsplit(upstream)
        if (self.origin.scheme not in ("http", "https") or not self.origin.hostname
                or self.origin.username or self.origin.password or self.origin.query
                or self.origin.fragment or self.origin.path not in ("", "/")):
            raise ValueError("--mcp-upstream must be a fixed http(s) origin without credentials or a path")
        self.port = self.origin.port
        self.slots = BoundedSemaphore(MAX_IN_FLIGHT)

    def handle(self, handler):
        url = urlsplit(handler.path)
        if url.path not in PATHS:
            return False
        handler.close_connection = True
        if not self.slots.acquire(blocking=False):
            handler.reply(503, {"error": "MCP relay is at capacity"})
            return True
        try:
            return self.forward(handler, url)
        finally:
            self.slots.release()

    def forward(self, handler, url):
        if url.scheme or url.netloc or url.query or url.fragment:
            handler.reply(400, {"error": "MCP requests require an exact endpoint path"})
            return True
        if handler.headers.get("transfer-encoding"):
            handler.reply(411, {"error": "Content-Length is required"})
            return True
        lengths = handler.headers.get_all("content-length", [])
        if len(lengths) > 1:
            handler.reply(400, {"error": "Ambiguous Content-Length"})
            return True
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            handler.reply(413, {"error": "MCP request exceeds the 1 MiB limit"})
            return True
        if handler.command == "POST" and not lengths:
            handler.reply(411, {"error": "Content-Length is required"})
            return True
        body = bytearray()
        deadline = monotonic() + BODY_TIMEOUT
        try:
            while len(body) < length:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                handler.connection.settimeout(remaining)
                chunk = handler.rfile.read1(min(length - len(body), 65536))
                if not chunk:
                    handler.reply(400, {"error": "Incomplete request body"})
                    return True
                body.extend(chunk)
        except TimeoutError:
            handler.reply(408, {"error": "MCP request body deadline exceeded"})
            return True
        handler.connection.settimeout(65)
        headers = {k: v for k, v in handler.headers.items() if k.lower() in REQUEST_HEADERS}
        headers["Content-Length"] = str(length)
        connection_type = http.client.HTTPSConnection if self.origin.scheme == "https" else http.client.HTTPConnection
        options = {"timeout": 65}
        if self.origin.scheme == "https":
            options["context"] = ssl.create_default_context()
        connection = connection_type(self.origin.hostname, self.port, **options)
        started = False
        try:
            connection.request(handler.command, url.path, body=body, headers=headers)
            upstream_socket = connection.sock
            response = connection.getresponse()
            if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() == "text/event-stream":
                upstream_socket.settimeout(None)
            handler.send_response(response.status)
            for key, value in response.getheaders():
                if (key.lower() in RESPONSE_HEADERS
                        or (key.lower() == "location" and url.path == "/.well-known/oauth-authorization-server")):
                    handler.send_header(key, value)
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.send_header("X-Accel-Buffering", "no")
            handler.end_headers()
            handler.close_connection = True
            started = True
            while chunk := response.read1(65536):
                handler.wfile.write(chunk)
                handler.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not started:
                handler.reply(502, {"error": "MCP upstream unavailable; action delivery may be unknown. Do not retry writes."})
        finally:
            connection.close()
        return True
