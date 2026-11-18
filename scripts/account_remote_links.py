"""Remote Control links for proxy sessions: HQ and its claude.ai mirror.

HQ's canonical Claude UUID is authoritative when present. Older native owners
expose only a cse route; those retain the existing exact assistant-message ID
correlation against recorded proxy traffic, with bounded retry of misses.
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
CLAUDE_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
MESSAGE_ID = re.compile(rb'"id":\s*"(msg_[A-Za-z0-9_]+)"')


def hub_sessions(hub_url, timeout=5):
    """HQ native routes, preserving their optional authoritative Claude UUID."""
    with urllib.request.urlopen(f"{hub_url.rstrip('/')}/api/sessions", timeout=timeout) as r:
        rows = json.loads(r.read()).get("sessions") or []
    result = []
    for row in rows:
        if row.get("transport") != "native":
            continue
        identity = row.get("claude_session_id")
        cse = row.get("session")
        if not isinstance(cse, str) or not cse.startswith("cse_"):
            cse = identity  # Older HQ snapshots carried the wire ID in this field.
        if not isinstance(cse, str) or not cse.startswith("cse_"):
            continue
        canonical = identity if isinstance(identity, str) and CLAUDE_UUID.fullmatch(identity) else None
        result.append({"id": row["id"], "cse": cse,
                       "claude_url": (row.get("upstream") or {}).get("session_url"),
                       "claude_session_id": canonical})
    return result


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
        for session in hub:
            canonical = session.get("claude_session_id")
            if canonical:
                # A native route can change conversation on /resume or /clear.
                # Its current owner binding supersedes a previously cached reply match.
                self.owners[session["cse"]] = canonical
                self.misses.pop(session["cse"], None)
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
