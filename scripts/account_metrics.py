"""Prometheus metrics for the dashboard's /metrics: account_insights.py's numbers as counters, histograms and gauges.

account_traffic.py inserts a flows row once, when its call ends, so rowid only grows and each scrape reads just the
rows added since the last one. Counters and histograms start when this process does, from the last ACTIVE_S of rows
(Prometheus reads that as a counter reset). Gauges come from the recorder's `live` snapshot and the active window;
a stale snapshot leaves the live gauges out (unknown, not zero) and sets cc_proxy_recorder_up to 0.
Labels are bounded on purpose: account, model and status, never a session id or an address.
"""

import runpy
import threading
import time
from pathlib import Path

_insights = runpy.run_path(str(Path(__file__).with_name("account_insights.py")), run_name="account_insights")
account_of, model_of, base_model = _insights["account_of"], _insights["model_of"], _insights["base_model"]
live_snapshot, ACTIVE_S, MIN_GENERATION_S = _insights["live_snapshot"], _insights["ACTIVE_S"], _insights["MIN_GENERATION_S"]

TTFT_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60)
SPEED_BUCKETS = (5, 10, 20, 30, 50, 75, 100, 150, 200, 300, 500)
TOKEN_TYPES = ("input", "cache_read", "output")
MAX_PAIRS = 256  # at most this many account/model pairs, plus one overflow pair
OVERFLOW = ("other", "other")


class Histogram:
    def __init__(self, buckets):
        self.buckets, self.counts, self.sum, self.count = buckets, [0] * len(buckets), 0.0, 0

    def observe(self, value):
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1
        self.sum += value
        self.count += 1


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.cursor = None  # last flows rowid counted
        self.requests, self.errors, self.fallbacks, self.tokens = {}, {}, {}, {}
        self.ttft, self.speed = {}, {}
        self.pairs = set()
        self.sessions, self.machines = {}, {}  # (account, session) / host -> latest started

    def pair(self, account, model):
        key = (account, model)
        if key in self.pairs:
            return key
        if len(self.pairs) >= MAX_PAIRS:
            return OVERFLOW
        self.pairs.add(key)
        return key

    def scrape(self, db, now=None):
        """The exposition text, after counting rows added since the last scrape. `db` None = no traffic database."""
        now = time.time() if now is None else now
        with self.lock:
            live = None
            if db is not None:
                self.count_new(db, now)
                live = live_snapshot(db, now)
            return render(self, live, now, db is not None)

    def count_new(self, db, now):
        newest = db.execute("SELECT max(rowid) FROM flows").fetchone()[0] or 0
        if self.cursor is None:  # first scrape only; retention must never rewind counters
            first = db.execute("SELECT min(rowid) FROM flows INDEXED BY flows_started WHERE started > ?", (now - ACTIVE_S,)).fetchone()[0]
            self.cursor = newest if first is None else first - 1
        columns = {row[1] for row in db.execute("PRAGMA table_info(flows)")}
        first_token = "first_token" if "first_token" in columns else "NULL"
        tokens = ("input_tokens, cache_read_tokens, output_tokens"
                  if {"input_tokens", "cache_read_tokens", "output_tokens"} <= columns else "NULL, NULL, NULL")
        rows = db.execute(
            f"SELECT rowid, started, finished, {first_token}, session_id, coalesce(client_host, client_ip, 'unknown'), "
            f"status, model, served_by, account, {tokens} FROM flows WHERE rowid > ? AND rowid <= ? "
            "AND model IS NOT NULL AND coalesce(url, '') NOT LIKE '%/v1/messages/count_tokens%'",
            (self.cursor, newest)).fetchall()
        for _, started, finished, first, session, host, status, model, served, placeholder, *counts in rows:
            actual = (account_of(served, placeholder), model_of(served, model))
            key = self.pair(*actual)
            code = str(status) if isinstance(status, int) and 100 <= status <= 599 else "none"
            self.requests[key + (code,)] = self.requests.get(key + (code,), 0) + 1
            self.errors[key] = self.errors.get(key, 0) + (status is None or status >= 400)
            self.fallbacks[key] = self.fallbacks.get(key, 0) + (bool(served) and base_model(model) != base_model(actual[1]))
            if counts[0] is not None:
                for kind, value in zip(TOKEN_TYPES, counts):
                    self.tokens[key + (kind,)] = self.tokens.get(key + (kind,), 0) + (value or 0)
            if first:
                self.ttft.setdefault(key, Histogram(TTFT_BUCKETS)).observe(first - started)
                if finished and counts[2] and finished - first >= MIN_GENERATION_S:
                    self.speed.setdefault(key, Histogram(SPEED_BUCKETS)).observe(counts[2] / (finished - first))
            if session:
                self.sessions[(key[0], session)] = max(started, self.sessions.get((key[0], session), 0))
            self.machines[host] = max(started, self.machines.get(host, 0))
        self.cursor = max(self.cursor, newest)
        for seen in (self.sessions, self.machines):
            for name in [name for name, started in seen.items() if now - started > ACTIVE_S]:
                del seen[name]


def labels(names, values):
    escape = lambda v: str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return "{" + ",".join(f'{n}="{escape(v)}"' for n, v in zip(names, values)) + "}" if names else ""


def family(lines, name, kind, help_text, samples, names=()):
    lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    lines += [f"{name}{labels(names, key)} {value}" for key, value in sorted(samples.items())]


def histogram(lines, name, help_text, series):
    lines += [f"# HELP {name} {help_text}", f"# TYPE {name} histogram"]
    for key, h in sorted(series.items()):
        for bound, count in zip(h.buckets + ("+Inf",), h.counts + [h.count]):
            lines.append(f"{name}_bucket{labels(('account', 'model', 'le'), key + (bound,))} {count}")
        lines.append(f"{name}_sum{labels(('account', 'model'), key)} {h.sum}")
        lines.append(f"{name}_count{labels(('account', 'model'), key)} {h.count}")


def render(m, live, now, has_db):
    pair = ("account", "model")
    lines = []
    family(lines, "cc_proxy_requests_total", "counter", "Model calls by answering account, model and HTTP status "
           "(none = no reply).", m.requests, pair + ("status",))
    family(lines, "cc_proxy_errors_total", "counter", "Model calls with no reply or a 4xx/5xx status.", m.errors, pair)
    family(lines, "cc_proxy_fallbacks_total", "counter", "Model calls answered by a different model than requested.",
           m.fallbacks, pair)
    family(lines, "cc_proxy_tokens_total", "counter", "Tokens the replies reported, by type (input, cache_read, "
           "output).", m.tokens, pair + ("type",))
    histogram(lines, "cc_proxy_ttft_seconds", "Time from request to the first streamed token.", m.ttft)
    histogram(lines, "cc_proxy_output_tokens_per_second", "Output tokens per second after the first token.", m.speed)
    active = {(account,): 0 for account, _ in m.errors}
    for account, _ in m.sessions:
        active[(account,)] = active.get((account,), 0) + 1
    family(lines, "cc_proxy_active_sessions", "gauge", f"Sessions with a model call in the last {ACTIVE_S // 60} "
           "minutes, by answering account.", active if has_db else {}, ("account",))
    family(lines, "cc_proxy_active_machines", "gauge", f"Client machines with a model call in the last "
           f"{ACTIVE_S // 60} minutes.", {(): len(m.machines)} if has_db else {})
    family(lines, "cc_proxy_recorder_up", "gauge", "1 when the traffic recorder's live snapshot is fresh.",
           {(): int(live is not None)})
    family(lines, "cc_proxy_traffic_db_up", "gauge", "1 when the traffic database is configured and readable.",
           {(): int(has_db)})
    if live is not None:
        inflight = {key: 0 for key in m.errors}  # every pair seen keeps a series, so idle reads 0, not gone
        for call in live["inflight"]:
            served = call.get("served_by")
            key = m.pair(account_of(served, call.get("account")), model_of(served, None))
            inflight[key] = inflight.get(key, 0) + 1
        family(lines, "cc_proxy_inflight", "gauge", "Model calls in flight now (model unknown until the reply "
               "headers).", inflight, pair)
        family(lines, "cc_proxy_open_connections", "gauge", "Client connections open now.",
               {(): len(live["connections"])})
        family(lines, "cc_proxy_connected_client_ips", "gauge", "Distinct client addresses connected now.",
               {(): len({ip for ip in live["connections"] if ip})})
        family(lines, "cc_proxy_live_snapshot_age_seconds", "gauge", "Age of the recorder's live snapshot.",
               {(): round(now - live["at"], 3)})
    return "\n".join(lines) + "\n"
