"""Route this CLI's native Remote Control sessions to an HQ backend.

mitmproxy owns transport. Never send Anthropic credentials to HQ, and never
redirect account sessions that were not registered through this proxy instance.
HQ's own upstream mirror marks its requests `x-hq-mirror: 1`; they go to
Anthropic, or the mirror's registration would loop back into HQ.
"""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from mitmproxy import ctx, exceptions, http


class NativeRemoteRoute:
    def __init__(self):
        self.sessions = {}

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

    def configure(self, updated):
        if not {"hq_remote_origin", "hq_remote_key_file"} & updated:
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
        mirror = req.headers.pop("x-hq-mirror", None) == "1"
        if (
            not mirror
            and req.scheme == "https"
            and req.host == "api.anthropic.com"
            and req.port == 443
        ):
            # map_remote may change the destination before the request hook.
            flow.metadata["hq_original_path"] = req.path

    def request(self, flow):
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
