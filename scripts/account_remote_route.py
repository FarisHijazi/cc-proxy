"""Route this CLI's native Remote Control sessions to an HQ backend.

mitmproxy owns transport. Never send Anthropic credentials to HQ, and never
redirect account sessions that were not registered through this proxy instance.
HQ's own upstream mirror marks its requests `x-hq-mirror: 1`; they go to
Anthropic, or the mirror's registration would loop back into HQ.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from mitmproxy import ctx, exceptions, http
from mitmproxy.proxy.mode_specs import RegularMode

EVENT_HOST = "session-events.cc-proxy.invalid"
EVENT_PATH = "/v1/code/sessions/mod-events"
MAX_EVENT_BODY = 256 * 1024
EVENT_HOST_PATTERN = r"^session-events\.cc-proxy\.invalid:443$"


class NativeRemoteRoute:
    def __init__(self):
        self.sessions = {}
        self.origin = ""
        self.key = ""

    def load(self, loader):
        loader.add_option(
            "hq_remote_origin", str, "", "HQ origin for native Remote Control"
        )
        loader.add_option(
            "hq_remote_key_file", str, "", "Private HQ registration key file"
        )
        loader.add_option(
            "hq_remote_sessions_file", str, "", "Private file keeping routed sessions across restarts"
        )

    def running(self):
        # Script reload retains the listener and its options. Extend only an
        # existing interception allowlist; an empty list already allows all.
        patterns = ctx.options.allow_hosts
        if patterns and not any(re.search(pattern, EVENT_HOST + ":443") for pattern in patterns):
            ctx.options.update(allow_hosts=[*patterns, EVENT_HOST_PATTERN])

    def configure(self, updated):
        if not {"hq_remote_origin", "hq_remote_key_file"} & updated:
            return
        if not ctx.options.hq_remote_origin and not ctx.options.hq_remote_key_file:
            self.origin, self.key = "", ""
            return
        try:
            origin = urlsplit(ctx.options.hq_remote_origin)
            if (
                origin.scheme not in {"http", "https"}
                or not origin.hostname
                or origin.username
                or origin.password
                or origin.path not in {"", "/"}
                or origin.query
                or origin.fragment
                or (
                    origin.scheme == "http"
                    and origin.hostname not in {"localhost", "127.0.0.1", "::1"}
                )
            ):
                raise ValueError()
            self.origin = ctx.options.hq_remote_origin.rstrip("/")
            self.key = Path(ctx.options.hq_remote_key_file).read_text().strip()
            if not 32 <= len(self.key) <= 1024 or not self.key.isascii():
                raise ValueError()
            # A shared --serve proxy outlives its clients: without this, a restart
            # would send live sessions' HQ worker tokens to Anthropic.
            self.store = ctx.options.hq_remote_sessions_file
            if self.store and Path(self.store).exists():
                self.sessions = json.loads(Path(self.store).read_text())
        except (OSError, ValueError):
            raise exceptions.OptionsError(
                "Invalid HQ origin or registration key file"
            ) from None

    def requestheaders(self, flow):
        req = flow.request
        if req.host == EVENT_HOST:
            flow.metadata["hq_mod_request"] = (req.scheme, req.port, req.path)
            req.stream = False
            return
        mirror = req.headers.pop("x-hq-mirror", None) == "1"
        if (
            self.origin and not mirror
            and req.scheme == "https"
            and req.host == "api.anthropic.com"
            and req.port == 443
        ):
            # map_remote may change the destination before the request hook.
            flow.metadata["hq_original_path"] = req.path

    def request(self, flow):
        if flow.response is not None:
            return
        if "hq_mod_request" in flow.metadata:
            self.mod_events(flow)
            return
        path = flow.metadata.get("hq_original_path", "")
        plain = path.split("?", 1)[0]
        create = plain == "/v1/code/sessions" and flow.request.method == "POST"
        if create:
            try:
                payload = json.loads(flow.request.content)
                if not isinstance(payload, dict) or "bridge" not in payload:
                    return
            except (ValueError, TypeError):
                return
            kind, sid = "create", None
        else:
            parts = plain.split("/")
            if len(parts) < 5 or parts[:4] != ["", "v1", "code", "sessions"]:
                return
            sid = parts[4]
            if sid not in self.sessions:
                return
            kind = "bridge" if parts[5:] == ["bridge"] else "session"
        # Only our generated worker credential can reach the local emulator.
        worker = sid and "/worker" in plain
        supplied = flow.request.headers.get("authorization", "")
        expected = self.sessions.get(sid)
        for header in ("authorization", "x-api-key", "cookie", "x-hq-native-key", "x-hq-private"):
            flow.request.headers.pop(header, None)
        # account_inject saw `+private` on the placeholder: HQ must not mirror it.
        if create and flow.metadata.get("account_private"):
            flow.request.headers["x-hq-private"] = "1"
        if worker:
            if not expected or supplied != "Bearer " + expected:
                flow.response = http.Response.make(
                    401, b"Invalid native worker credential"
                )
                return
            flow.request.headers["authorization"] = supplied
        else:
            flow.request.headers["x-hq-native-key"] = self.key
        flow.request.url = self.origin + path
        flow.metadata["hq_remote"] = (kind, sid)

    def mod_events(self, flow):
        # The regular proxy's existing private-network/tunnel boundary is the
        # caller's authority. Reverse API keys cannot grant native observation.
        if not isinstance(flow.client_conn.proxy_mode, RegularMode):
            flow.response = http.Response.make(403, b"Session events require the private forward proxy")
            return
        if flow.metadata["hq_mod_request"] != ("https", 443, EVENT_PATH):
            flow.response = http.Response.make(404, b"Unknown session event endpoint")
            return
        if flow.request.method != "POST":
            flow.response = http.Response.make(405, b"Session events require POST")
            return
        if not self.origin:
            flow.response = http.Response.make(503, b"This proxy has no native HQ event route")
            return
        try:
            body = flow.request.content
            if body is None or len(body) > MAX_EVENT_BODY:
                flow.response = http.Response.make(413, b"Session event batch is too large")
                return
            payload = json.loads(body)
            sid = payload.get("native_session_id") if isinstance(payload, dict) else None
            if not isinstance(sid, str) or sid not in self.sessions:
                flow.response = http.Response.make(404, b"Native session is not owned by this proxy")
                return
        except (ValueError, TypeError):
            flow.response = http.Response.make(400, b"Invalid session event batch")
            return
        # Do not copy account, API, worker, cookie, or caller-supplied HQ secrets.
        flow.request.headers = http.Headers([(b"content-type", b"application/json")])
        flow.request.content = body
        flow.request.headers["x-hq-native-key"] = self.key
        flow.request.url = self.origin + EVENT_PATH
        flow.request.headers["host"] = urlsplit(self.origin).netloc

    def responseheaders(self, flow):
        if flow.metadata.get("hq_remote", ("",))[0] in {"create", "bridge"}:
            flow.response.stream = False

    def response(self, flow):
        marker = flow.metadata.get("hq_remote")
        if not marker or not 200 <= flow.response.status_code < 300:
            return
        kind, sid = marker
        try:
            if kind == "create":
                value = json.loads(flow.response.content)["session"]["id"]
                if isinstance(value, str) and value.startswith("cse_"):
                    self.sessions[value] = None
            elif kind == "bridge":
                value = json.loads(flow.response.content)["worker_jwt"]
                if isinstance(value, str) and value:
                    self.sessions[sid] = value
        except (ValueError, TypeError, KeyError):
            return  # Native CLI rejects an invalid handshake; no upstream fallback.
        if getattr(self, "store", ""):
            self.sessions = dict(list(self.sessions.items())[-1000:])
            temporary = self.store + ".tmp"
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
                json.dump(self.sessions, out)
            os.replace(temporary, self.store)


addons = [NativeRemoteRoute()]
