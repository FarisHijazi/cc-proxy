"""Record every flow through the --serve proxy in SQLite, with who sent it.

Enabled by the accounts file's "traffic_db" path. Secrets never reach the database:
credential headers are dropped and JSON keys that hold a credential are redacted, so
the placeholder (`proxy-account-<alias>...`) is the only account trace. Bodies are
decoded and stored zlib-compressed. A model request resends the whole conversation
every turn, so its `system`, `tools` and each message are stored once in `blobs` and
the request body keeps `{"$blob": hash}` in their place. Read them with
`scripts/claude-traffic`, which puts the pieces back.
"""

import hashlib
import json
import re
import sqlite3
import subprocess
import time
import zlib
from pathlib import Path
from urllib.parse import unquote

from mitmproxy.net import encoding

# Set by `claude-account-router --via` clients; stripped here so they go no further.
CLIENT_HEADERS = {
    "x-claude-client-host": "client_host",
    "x-claude-client-user": "client_user",
    "x-claude-client-cwd": "launch_cwd",
}
SERVED_BY_HEADER = "x-claude-codex-served-by"
SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
SECRET_KEYS = {
    "access_token", "refresh_token", "id_token", "oauth_token", "token",
    "api_key", "client_secret", "password", "code_verifier",
}
CWD = re.compile(r"(?:Primary working directory|Working directory|Current working directory):\s*([^\n<]+)")
SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    id TEXT PRIMARY KEY, started REAL, finished REAL,
    client_ip TEXT, client_host TEXT, client_user TEXT, launch_cwd TEXT, cwd TEXT,
    session_id TEXT, account TEXT, served_by TEXT, model TEXT,
    method TEXT, url TEXT, status INTEGER, error TEXT,
    request_headers TEXT, request_body BLOB, response_headers TEXT, response_body BLOB
);
CREATE INDEX IF NOT EXISTS flows_started ON flows(started);
CREATE INDEX IF NOT EXISTS flows_session ON flows(session_id);
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, body BLOB, last_seen REAL);
CREATE TABLE IF NOT EXISTS websocket_messages (
    flow_id TEXT, at REAL, from_client INTEGER, body BLOB
);
CREATE INDEX IF NOT EXISTS websocket_flow ON websocket_messages(flow_id);
"""


NAMES_TTL_S = 300
_names = (0.0, {})


def tailnet_name(ip):
    """Name a client that sent no machine header (launched before it existed) by its tailnet IP."""
    global _names
    if time.time() - _names[0] > NAMES_TTL_S:
        names = {}
        try:
            status = json.loads(subprocess.run(
                ["tailscale", "status", "--json"], capture_output=True, timeout=5, check=True
            ).stdout)
            for peer in [status["Self"], *status.get("Peer", {}).values()]:
                for address in peer.get("TailscaleIPs", []):
                    names[address] = peer["HostName"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        _names = (time.time(), names)
    return _names[1].get(ip)


def texts(value):
    """Every text in a system prompt or message content, as Claude Code sends it."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from texts(item)
    elif isinstance(value, dict):
        yield from texts(value.get("text") or value.get("content") or "")


def working_directory(request):
    """The session's folder, from the environment block Claude Code writes into the prompt."""
    messages = request.get("messages") or [{}]
    # Newer Claude Code sends it as a system-role message; older ones in `system`
    # or the first user message. Later messages may quote other folders.
    places = [request.get("system", ""), *(m.get("content", "") for m in messages if m.get("role") == "system")]
    for text in texts([*places, messages[0].get("content", "")]):
        match = CWD.search(text)
        if match:
            return match.group(1).strip()
    return None


def redact(value):
    if isinstance(value, dict):
        return {k: "[redacted]" if k in SECRET_KEYS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def clean_body(raw, content_encoding=""):
    """Decoded body with credential fields redacted, compressed for storage."""
    if not raw:
        return None
    try:
        raw = encoding.decode(raw, content_encoding) if content_encoding else raw
    except ValueError:
        pass
    try:
        raw = json.dumps(redact(json.loads(raw)), ensure_ascii=False).encode()
    except ValueError:
        pass  # SSE, binary or plain text: no credential fields to find
    return zlib.compress(raw)


def clean_headers(headers):
    return json.dumps(
        [(k, "[redacted]" if k.lower() in SECRET_HEADERS else v) for k, v in headers.items(multi=True)]
    )


PRUNE_EVERY_S = 3600


class TrafficDB:
    def __init__(self, path_of, days_of=lambda: None):
        self.path_of = path_of  # the accounts file's "traffic_db", read live
        self.days_of = days_of  # its "traffic_days": keep this many days, unset keeps all
        self.path = self.db = None
        self.pruned = 0.0

    def connection(self):
        path = self.path_of()
        if path != self.path:
            if self.db is not None:
                self.db.close()
            self.path, self.db = path, None
            if path:
                Path(path).expanduser().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.db = sqlite3.connect(Path(path).expanduser(), isolation_level=None)
                self.db.execute("PRAGMA journal_mode=WAL")
                self.db.executescript(SCHEMA)
                try:  # databases written before blobs had last_seen
                    self.db.execute("ALTER TABLE blobs ADD COLUMN last_seen REAL")
                    self.db.execute("UPDATE blobs SET last_seen = ?", (time.time(),))
                except sqlite3.OperationalError:
                    pass
        return self.db

    def prune(self):
        """Drop what is older than traffic_days. A blob a kept flow uses was seen after that cutoff."""
        days = self.days_of()
        if not days or time.time() - self.pruned < PRUNE_EVERY_S:
            return
        self.pruned = time.time()
        cutoff = self.pruned - float(days) * 86400
        self.db.execute("DELETE FROM flows WHERE started < ?", (cutoff,))
        self.db.execute("DELETE FROM websocket_messages WHERE at < ?", (cutoff,))
        self.db.execute("DELETE FROM blobs WHERE last_seen < ?", (cutoff,))

    def requestheaders(self, flow):
        if self.connection() is None:
            return
        headers = flow.request.headers
        supplied = headers.get("authorization", "")
        meta = {
            "started": time.time(),
            "client_ip": flow.client_conn.peername[0] if flow.client_conn.peername else None,
            "session_id": headers.get("x-claude-code-session-id"),
            # Before AccountInject swaps it and map-remote points it at the router.
            "account": supplied.removeprefix("Bearer ") if "proxy-account-" in supplied
            else ("other-credential" if supplied else None),
            "url": flow.request.pretty_url,
            "request_headers": clean_headers(headers),
        }
        for header, column in CLIENT_HEADERS.items():
            if header in headers:
                meta[column] = unquote(headers.pop(header))
        meta.setdefault("client_host", tailnet_name(meta["client_ip"]))
        flow.metadata["traffic"] = meta

    def responseheaders(self, flow):
        if "traffic" in flow.metadata and flow.response.stream:
            chunks = flow.metadata["traffic"]["chunks"] = []

            def tee(data):
                chunks.append(data)
                return data

            flow.response.stream = tee

    def response(self, flow):
        self.record(flow)

    def error(self, flow):
        self.record(flow)

    def deduplicated(self, request):
        """Swap the parts a conversation repeats every turn for references to `blobs`."""
        def stored(value):
            raw = json.dumps(redact(value), ensure_ascii=False, sort_keys=True).encode()
            digest = hashlib.sha256(raw).hexdigest()
            self.db.execute(
                "INSERT INTO blobs VALUES (?, ?, ?) ON CONFLICT(hash) DO UPDATE SET last_seen = excluded.last_seen",
                (digest, zlib.compress(raw), time.time()),
            )
            return {"$blob": digest}

        request = dict(request)
        for key in ("system", "tools"):
            if key in request:
                request[key] = stored(request[key])
        if isinstance(request.get("messages"), list):
            request["messages"] = [stored(message) for message in request["messages"]]
        return zlib.compress(json.dumps(redact(request), ensure_ascii=False).encode())

    def record(self, flow):
        meta = flow.metadata.pop("traffic", None)
        db = self.connection()
        if meta is None or db is None:
            return
        self.prune()
        request, response = flow.request, flow.response
        body = request.get_content(strict=False) or b""
        model = cwd = None
        request_body = clean_body(body)
        try:
            parsed = json.loads(body or b"{}")
            if isinstance(parsed, dict) and parsed.get("model"):
                model, cwd = parsed["model"], working_directory(parsed)
                request_body = self.deduplicated(parsed)
        except (ValueError, AttributeError):
            pass
        if response is not None:
            raw = b"".join(meta["chunks"]) if "chunks" in meta else response.raw_content
            response_body = clean_body(raw, response.headers.get("content-encoding", ""))
        db.execute(
            "INSERT OR REPLACE INTO flows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                flow.id, meta["started"], time.time(),
                meta["client_ip"], meta.get("client_host"), meta.get("client_user"), meta.get("launch_cwd"), cwd,
                meta["session_id"], meta["account"],
                response.headers.get(SERVED_BY_HEADER) if response is not None else None, model,
                request.method, meta["url"], response.status_code if response is not None else None,
                flow.error.msg if flow.error else None,
                meta["request_headers"], request_body,
                clean_headers(response.headers) if response is not None else None,
                response_body if response is not None else None,
            ),
        )

    def websocket_message(self, flow):
        db = self.connection()
        if db is None:
            return
        message = flow.websocket.messages[-1]
        db.execute(
            "INSERT INTO websocket_messages VALUES (?,?,?,?)",
            (flow.id, message.timestamp, message.from_client, clean_body(message.content)),
        )
