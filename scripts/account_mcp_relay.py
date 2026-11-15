"""Expose the existing authenticated fleet MCP on cc-proxy's dashboard listener."""

import http.client
import ssl
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


class MCPRelay:
    def __init__(self, upstream):
        self.origin = urlsplit(upstream)
        if (self.origin.scheme not in ("http", "https") or not self.origin.hostname
                or self.origin.username or self.origin.password or self.origin.query
                or self.origin.fragment or self.origin.path not in ("", "/")):
            raise ValueError("--mcp-upstream must be a fixed http(s) origin without credentials or a path")
        self.port = self.origin.port

    def handle(self, handler):
        url = urlsplit(handler.path)
        if url.path not in PATHS:
            return False
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
        handler.connection.settimeout(65)
        body = handler.rfile.read(length)
        if len(body) != length:
            handler.reply(400, {"error": "Incomplete request body"})
            return True
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
            response = connection.getresponse()
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
