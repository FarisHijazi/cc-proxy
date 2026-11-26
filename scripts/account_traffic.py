"""Record every flow through the --serve proxy in SQLite, with who sent it.

Enabled by the accounts file's "traffic_db" path. Secrets never reach the database:
credential headers are dropped and JSON keys that hold a credential are redacted, so
the placeholder (`proxy-account-<alias>...`) is the only account trace. Bodies are
decoded and stored zlib-compressed. A model request resends the whole conversation
every turn, so its `system`, `tools` and each message are stored once in `blobs` and
the request body keeps `{"$blob": hash}` in their place. Read them with
`scripts/claude-traffic`, which puts the pieces back.

mitmproxy runs every hook on ONE event loop shared by every connection of every
account, so the hooks only copy plain data into a bounded queue. One writer thread
owns the database and does all decoding, redaction, deduplication, pruning and
inserts. When it falls behind, rows are dropped: telemetry never delays traffic.

Each row also keeps `first_byte` (reply headers) and `first_token` (first streamed content delta),
so the dashboard derives TTFT and tokens/s. Open client connections and in-flight model calls live
only in memory; the writer thread copies them into the one-row `live` table every LIVE_EVERY_S.
"""

import hashlib
import json
import queue
import re
import runpy
import sqlite3
import subprocess
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import unquote

from mitmproxy.net import encoding

reply_tokens = runpy.run_path(str(Path(__file__).with_name("account_providers.py")))["reply_tokens"]
TOKEN_COLUMNS = ("input_tokens", "cache_read_tokens", "output_tokens", "cache_write_tokens", "cache_write_1h_tokens")
TIMING_COLUMNS = ("first_byte", "first_token")
FLOW_COLUMNS = (
    "id, started, finished, client_ip, client_host, client_user, launch_cwd, cwd, session_id, account, "
    "served_by, model, method, url, status, error, request_headers, request_body, response_headers, "
    "response_body, api_key, " + ", ".join(TOKEN_COLUMNS + TIMING_COLUMNS)
)
# Anthropic SSE as every backend's translation emits it: the first chunk carrying one is the first token.
CONTENT_DELTA = b"content_block_delta"
MODEL_CALL = re.compile(r"/v1/messages(?:\?|$)")

# Set by `claude-account-router --via` clients; stripped here so they go no further.
CLIENT_HEADERS = {
    "x-claude-client-host": "client_host",
    "x-claude-client-user": "client_user",
    "x-claude-client-cwd": "launch_cwd",
}
SERVED_BY_HEADER = "x-claude-codex-served-by"
# The account pool carries every login and Console key; only the router may read it.
SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key",
                  "x-claude-codex-account-pool"}
SECRET_KEYS = {
    "access_token", "refresh_token", "id_token", "oauth_token", "token",
    "api_key", "client_secret", "password", "code_verifier",
    "syncToken", "assetToken", "cap",
}
WS_PROTOCOL = re.compile(r"[a-z][a-z0-9-]*\.v\d+")
CWD = re.compile(r"(?:Primary working directory|Working directory|Current working directory):\s*([^\n<]+)")
SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    id TEXT PRIMARY KEY, started REAL, finished REAL,
    client_ip TEXT, client_host TEXT, client_user TEXT, launch_cwd TEXT, cwd TEXT,
    session_id TEXT, account TEXT, served_by TEXT, model TEXT,
    method TEXT, url TEXT, status INTEGER, error TEXT,
    request_headers TEXT, request_body BLOB, response_headers TEXT, response_body BLOB, api_key TEXT,
    input_tokens INTEGER, cache_read_tokens INTEGER, output_tokens INTEGER,
    cache_write_tokens INTEGER, cache_write_1h_tokens INTEGER, first_byte REAL, first_token REAL
);
CREATE INDEX IF NOT EXISTS flows_started ON flows(started);
CREATE INDEX IF NOT EXISTS flows_session ON flows(session_id);
CREATE TABLE IF NOT EXISTS blobs (hash TEXT PRIMARY KEY, body BLOB, last_seen REAL);
CREATE TABLE IF NOT EXISTS websocket_messages (
    flow_id TEXT, at REAL, from_client INTEGER, body BLOB
);
CREATE INDEX IF NOT EXISTS websocket_flow ON websocket_messages(flow_id);
CREATE TABLE IF NOT EXISTS live (name TEXT PRIMARY KEY, at REAL, body TEXT);
"""


ACCOUNT_INDEX_SQL = """CREATE INDEX IF NOT EXISTS flows_answered_account
ON flows(served_by COLLATE NOCASE, started) WHERE status = 200 AND served_by IS NOT NULL"""


def account_index(db, budget_s=3):
    """Bound schema maintenance below the live writer's five-second busy timeout."""
    deadline = time.monotonic() + budget_s
    busy_ms = db.execute("PRAGMA busy_timeout").fetchone()[0]
    db.execute("PRAGMA busy_timeout=0")
    db.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
    try:
        db.execute(ACCOUNT_INDEX_SQL)
        return True
    except sqlite3.OperationalError as error:
        if str(error) != "interrupted" and error.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            raise
        return False
    finally:
        db.set_progress_handler(None, 0)
        db.execute(f"PRAGMA busy_timeout={busy_ms}")


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


def clean_header(name, value):
    name = name.lower()
    if name in SECRET_HEADERS:
        return "[redacted]"
    if name == "sec-websocket-protocol":
        # Artifact watches send their token as a subprotocol after the protocol name.
        return ",".join(p if WS_PROTOCOL.fullmatch(p.strip()) else "[redacted]" for p in value.split(","))
    return value


def clean_headers(items):
    return json.dumps([(k, clean_header(k, v)) for k, v in items])


PRUNE_EVERY_S = 3600
PRUNE_BATCH_ROWS = 256
PRUNE_TABLES = (("flows", "started"), ("websocket_messages", "at"), ("blobs", "last_seen"))
QUEUE_MAX = 5000
LIVE_EVERY_S = 5
INFLIGHT_MAX_S = 3600


class TrafficDB:
    def __init__(self, path_of, days_of=lambda: None):
        self.path_of = path_of  # the accounts file's "traffic_db", read live
        self.days_of = days_of  # its "traffic_days": keep this many days, unset keeps all
        self.path = self.db = None
        self.pruned = 0.0
        self.prune_state = None
        self.index_pending, self.index_retry_at = False, 0.0
        self.dropped = 0
        self.jobs = queue.Queue(QUEUE_MAX)
        self.live_lock = threading.Lock()
        self.clients, self.inflight = {}, {}  # connection id -> ip; flow id -> who and since when
        self.live_at = 0.0
        threading.Thread(target=self.writer, name="traffic-db", daemon=True).start()

    def enqueue(self, path, job, *args):
        try:
            self.jobs.put_nowait((path, job, args))
        except queue.Full:
            self.dropped += 1

    def flush(self):
        """Wait until every queued row is written (tests, shutdown)."""
        self.jobs.join()

    def writer(self):
        while True:
            try:
                path, job, args = self.jobs.get(timeout=LIVE_EVERY_S)
            except queue.Empty:
                path = job = None
            try:
                if job is not None and self.connection(path) is not None:
                    job(*args)
                if job is None and self.connection() is not None:
                    self.prune()
                if self.db is not None and self.jobs.empty():
                    self.maintain_index()
                if time.time() - self.live_at >= LIVE_EVERY_S and self.connection() is not None:
                    self.write_live()
            except Exception as error:  # noqa: BLE001 - a bad row must not stop the recorder
                print(f"traffic-db: {type(error).__name__}: {error}", flush=True)
            finally:
                if job is not None:
                    self.jobs.task_done()

    def write_live(self):
        self.live_at = time.time()
        with self.live_lock:
            for flow_id in [k for k, v in self.inflight.items() if self.live_at - v["started"] > INFLIGHT_MAX_S]:
                del self.inflight[flow_id]  # a flow mitmproxy never finished must not count forever
            body = {"connections": list(self.clients.values()), "inflight": [dict(v) for v in self.inflight.values()]}
        body.update(dropped=self.dropped, queued=self.jobs.qsize())
        self.db.execute("INSERT OR REPLACE INTO live VALUES ('now', ?, ?)", (self.live_at, json.dumps(body)))

    def client_connected(self, client):
        with self.live_lock:
            self.clients[client.id] = client.peername[0] if client.peername else None

    def client_disconnected(self, client):
        with self.live_lock:
            self.clients.pop(client.id, None)

    def connection(self, path=None):
        path = self.path_of() if path is None else path
        if path != self.path:
            if self.db is not None:
                self.db.close()
            self.path, self.db = path, None
            self.pruned, self.prune_state = 0.0, None
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
                present = {row[1] for row in self.db.execute("PRAGMA table_info(flows)")}
                for column in TOKEN_COLUMNS + TIMING_COLUMNS:
                    if column not in present:
                        kind = "REAL" if column in TIMING_COLUMNS else "INTEGER"
                        self.db.execute(f"ALTER TABLE flows ADD COLUMN {column} {kind}")
                if "api_key" not in present:
                    self.db.execute("ALTER TABLE flows ADD COLUMN api_key TEXT")
                self.index_pending, self.index_retry_at = True, 0.0
        return self.db

    def maintain_index(self):
        if self.index_pending and time.time() >= self.index_retry_at:
            self.index_pending = not account_index(self.db)
            self.index_retry_at = time.time() + 300
            if self.index_pending:
                print("traffic-db: account index deferred: maintenance exceeded three seconds", flush=True)

    def prune(self):
        """Advance retention by one bounded rowid batch, never a whole-table startup scan."""
        days = self.days_of()
        if not days:
            self.prune_state = None
            return
        if self.prune_state is None:
            if time.time() - self.pruned < PRUNE_EVERY_S:
                return
            cutoff = time.time() - float(days) * 86400
            upper = [self.db.execute(f"SELECT coalesce(max(rowid), 0) FROM {table}").fetchone()[0]
                     for table, _ in PRUNE_TABLES]
            self.prune_state = (cutoff, upper, 0, 0)
        cutoff, upper, index, cursor = self.prune_state
        table, column = PRUNE_TABLES[index]
        rows = self.db.execute(
            f"SELECT rowid FROM {table} WHERE rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
            (cursor, upper[index], PRUNE_BATCH_ROWS),
        ).fetchall()
        if rows:
            end = rows[-1][0]
            # Recheck age at deletion: a deduplicated blob may have been reused since the sweep began.
            self.db.execute(
                f"DELETE FROM {table} NOT INDEXED WHERE rowid > ? AND rowid <= ? AND {column} < ?",
                (cursor, end, min(cutoff, time.time() - float(days) * 86400)),
            )
            self.prune_state = (cutoff, upper, index, end)
        else:
            index += 1
            self.prune_state = (cutoff, upper, index, 0) if index < len(PRUNE_TABLES) else None
            if self.prune_state is None:
                self.pruned = time.time()

    def requestheaders(self, flow):
        path = self.path_of()
        if not path:
            return
        headers = flow.request.headers
        supplied = headers.get("authorization", "")
        meta = {
            "path": path,
            "started": time.time(),
            "client_ip": flow.client_conn.peername[0] if flow.client_conn.peername else None,
            "session_id": headers.get("x-claude-code-session-id"),
            # Before AccountInject swaps it and map-remote points it at the router.
            "account": supplied.removeprefix("Bearer ") if "proxy-account-" in supplied
            else ("other-credential" if supplied else None),
            "url": flow.request.pretty_url,
            "request_headers": list(headers.items(multi=True)),
        }
        for header, column in CLIENT_HEADERS.items():
            if header in headers:
                meta[column] = unquote(headers.pop(header))
        flow.metadata["traffic"] = meta
        if flow.request.method == "POST" and MODEL_CALL.search(flow.request.path):
            with self.live_lock:
                self.inflight[flow.id] = {"started": meta["started"], "client": meta["client_ip"],
                                          "host": meta.get("client_host"), "account": meta["account"],
                                          "session_id": meta["session_id"], "served_by": None}

    def responseheaders(self, flow):
        meta = flow.metadata.get("traffic")
        if meta is None:
            return
        meta["first_byte"] = time.time()
        with self.live_lock:
            if flow.id in self.inflight:
                self.inflight[flow.id]["served_by"] = flow.response.headers.get(SERVED_BY_HEADER)
        if flow.response.stream:
            chunks = meta["chunks"] = []
            # The Anthropic passthrough relays Anthropic's gzip as is; inflate only until the first delta.
            gzip = zlib.decompressobj(16 + zlib.MAX_WBITS) if flow.response.headers.get("content-encoding") == "gzip" else None

            def tee(data):
                nonlocal gzip
                chunks.append(data)
                if "first_token" not in meta:
                    try:
                        found = CONTENT_DELTA in (gzip.decompress(data) if gzip else data)
                    except zlib.error:
                        found, gzip = False, None
                        meta["first_token"] = None  # undecodable: unknown, and stop looking
                    if found:
                        meta["first_token"] = time.time()
                        gzip = None
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
        """On the event loop: copy what the row needs, nothing else."""
        meta = flow.metadata.pop("traffic", None)
        with self.live_lock:
            self.inflight.pop(flow.id, None)
        if meta is None:
            return
        request, response = flow.request, flow.response
        meta.update(
            id=flow.id, finished=time.time(), method=request.method, api_key=flow.metadata.get("api_key"),
            request_raw=request.raw_content or b"",
            request_encoding=request.headers.get("content-encoding", ""),
            error=flow.error.msg if flow.error else None,
        )
        if response is not None:
            meta.update(
                status=response.status_code,
                response_raw=b"".join(meta.pop("chunks")) if "chunks" in meta else response.raw_content,
                response_headers=list(response.headers.items(multi=True)),
            )
        meta.pop("chunks", None)
        self.enqueue(meta["path"], self.write_flow, meta)

    def write_flow(self, meta):
        """On the writer thread: decode, redact, deduplicate and insert one flow."""
        self.prune()
        body = meta["request_raw"]
        if meta["request_encoding"]:
            try:
                body = encoding.decode(body, meta["request_encoding"])
            except ValueError:
                pass
        model = cwd = None
        request_body = clean_body(body)
        try:
            parsed = json.loads(body or b"{}")
            if isinstance(parsed, dict) and parsed.get("model"):
                model, cwd = parsed["model"], working_directory(parsed)
                request_body = self.deduplicated(parsed)
        except (ValueError, AttributeError):
            pass
        tokens = response_body = response_headers = served_by = None
        if "response_headers" in meta:
            items = meta["response_headers"]
            lower = {k.lower(): v for k, v in items}
            response_body = clean_body(meta["response_raw"], lower.get("content-encoding", ""))
            response_headers, served_by = clean_headers(items), lower.get(SERVED_BY_HEADER)
            tokens = reply_tokens(response_body) if model else None
        self.db.execute(
            f"INSERT OR REPLACE INTO flows ({FLOW_COLUMNS}) VALUES ({','.join('?' * 28)})",
            (
                meta["id"], meta["started"], meta["finished"],
                meta["client_ip"], meta.get("client_host") or tailnet_name(meta["client_ip"]),
                meta.get("client_user"), meta.get("launch_cwd"), cwd,
                meta["session_id"], meta["account"], served_by, model,
                meta["method"], meta["url"], meta.get("status"), meta["error"],
                clean_headers(meta["request_headers"]), request_body, response_headers,
                response_body, meta["api_key"],
                *((tokens["input"], tokens["cache_read"], tokens["output"], tokens["cache_write"], tokens["cache_write_1h"])
                  if tokens else (None,) * 5),
                meta.get("first_byte"), meta.get("first_token"),
            ),
        )

    def websocket_message(self, flow):
        path = self.path_of()
        if not path:
            return
        message = flow.websocket.messages[-1]
        self.enqueue(path, self.write_message, flow.id, message.timestamp, message.from_client, message.content)

    def write_message(self, flow_id, at, from_client, content):
        self.db.execute("INSERT INTO websocket_messages VALUES (?,?,?,?)", (flow_id, at, from_client, clean_body(content)))
