"""Remote Control links for proxy sessions: the session's page on HQ (cchq) and its claude.ai mirror.

HQ lists the native Remote Control sessions it hosts with their claude.ai mirror URL, but Remote
Control traffic never names the Claude Code session: the CLI replaces the session id with the
Remote Control id. What both share is the reply: a worker event relays an assistant message with
the `msg_…` id of the model reply that the traffic database recorded for the session moments earlier.
A link, once found, never changes, so it is kept; an unmatched session is retried after RETRY_S.
"""

import json
import re
import time
import urllib.parse
import urllib.request
import zlib

RC_PREFIX = "https://api.anthropic.com/v1/code/sessions/"
EVENTS_SUFFIX = "/worker/events"
EVENT_FLOWS, REPLY_WINDOW_S, RETRY_S = 40, 180, 300
MESSAGE_ID = re.compile(rb'"id":\s*"(msg_[A-Za-z0-9_]+)"')


def hub_sessions(hub_url, timeout=5):
    """HQ's native sessions, each {"id", "cse", "claude_url"}."""
    with urllib.request.urlopen(f"{hub_url.rstrip('/')}/api/sessions", timeout=timeout) as r:
        rows = json.loads(r.read()).get("sessions") or []
    return [{"id": s["id"], "cse": s["claude_session_id"], "claude_url": (s.get("upstream") or {}).get("session_url")}
            for s in rows if s.get("transport") == "native" and str(s.get("claude_session_id", "")).startswith("cse_")]


def _body(blob):
    try:
        return zlib.decompress(blob) if blob else b""
    except zlib.error:
        return blob if isinstance(blob, bytes) else b""


def _replies(body):
    """Reply ids of the assistant messages a worker-events upload relays."""
    try:
        events = json.loads(body).get("events") or []
    except (ValueError, AttributeError):
        return []
    return [p["message"]["id"] for p in (e.get("payload") or {} for e in events if isinstance(e, dict))
            if isinstance(p, dict) and p.get("type") == "assistant" and str((p.get("message") or {}).get("id", "")).startswith("msg_")]


def owner(db, flows):
    """The session whose recorded reply carries a message id relayed in these worker-event flows (newest first)."""
    for flow_id, started in flows:
        row = db.execute("SELECT request_body FROM flows WHERE id = ?", (flow_id,)).fetchone()
        for reply in _replies(_body(row[0] if row else None)):
            needle = reply.encode()
            for sid, blob in db.execute(
                    "SELECT session_id, response_body FROM flows WHERE started BETWEEN ? AND ? "
                    "AND session_id IS NOT NULL AND model IS NOT NULL ORDER BY started DESC",
                    (started - REPLY_WINDOW_S, started)):
                if needle in _body(blob):
                    return sid
    return None


class RemoteLinks:
    """Claude Code session id -> {"cchq": url, "claude": url or None}, the newest Remote Control session winning."""

    def __init__(self, hub_url, public_url):
        self.hub_url, self.public_url = hub_url, public_url.rstrip("/")
        self.owners, self.misses = {}, {}

    def __call__(self, db, days):
        hub = hub_sessions(self.hub_url)
        now = time.time()
        wanted = {s["cse"] for s in hub if s["cse"] not in self.owners and now - self.misses.get(s["cse"], 0) >= RETRY_S}
        flows, newest = {}, {}
        for flow_id, url, started in db.execute(
                "SELECT id, url, started FROM flows WHERE started > ? AND url LIKE ? ORDER BY started DESC",
                (now - days * 86400, RC_PREFIX + "cse_%")):
            cse = url[len(RC_PREFIX):].split("/", 1)[0]
            newest.setdefault(cse, started)
            if cse in wanted and url.endswith(EVENTS_SUFFIX) and len(flows.setdefault(cse, [])) < EVENT_FLOWS:
                flows[cse].append((flow_id, started))
        for cse in wanted:
            sid = owner(db, flows.get(cse, []))
            if sid:
                self.owners[cse] = sid
                self.misses.pop(cse, None)
            else:
                self.misses[cse] = now
        found = {}
        for s in hub:
            sid, seen = self.owners.get(s["cse"]), newest.get(s["cse"], 0)
            if sid and (sid not in found or found[sid][0] < seen):
                found[sid] = (seen, {"cchq": f"{self.public_url}/#session={urllib.parse.quote(s['id'])}",
                                     "claude": s["claude_url"]})
        return {sid: link for sid, (_, link) in found.items()}
