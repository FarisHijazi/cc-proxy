#!/usr/bin/env python3
"""cc-proxy dashboard: accounts, live usage, traffic and settings of a --serve proxy.

It edits the proxy's accounts file, which the proxy reloads on its next request, and reads
the traffic database (account_traffic.py). Usage and profiles come through the proxy with
each alias's placeholder, so this process never holds a Claude token. There is no login:
bind it to the same private address as the proxy.
"""

import argparse
import json
import runpy
import shutil
import socket
import sqlite3
import ssl
import time
import urllib.request
import zlib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
STRATEGIES = ["session", "prefer", "round-robin", "least-sessions"]
WINDOWS = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]
CACHE_S = 60
COLUMNS = "id, started, client_host, client_ip, client_user, cwd, launch_cwd, account, served_by, model, status, method, url"


def inference_only(error):
    """A `claude setup-token` may only call the model, so usage and profile answer 401/403."""
    return bool(error) and ("401" in error or "403" in error)


def reply_usage(headers):
    """Usage windows from the rate-limit headers Anthropic sends with every reply."""
    h = {k.lower(): v for k, v in headers}
    windows = {}
    for name, key in (("five_hour", "5h"), ("seven_day", "7d")):
        used = h.get(f"anthropic-ratelimit-unified-{key}-utilization")
        if used is not None:
            reset = h.get(f"anthropic-ratelimit-unified-{key}-reset")
            windows[name] = {
                "utilization": float(used) * 100,
                "resets_at": datetime.fromtimestamp(int(reset), timezone.utc).isoformat() if reset else None,
            }
    return windows


def alias_of(placeholder):
    return (placeholder or "").removeprefix("proxy-account-").split("+")[0] or None


class Dashboard:
    def __init__(self, accounts_file, proxy, ca):
        self.accounts_file = accounts_file
        self.proxy = urlsplit(proxy)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": proxy}),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))),
        )
        self.cache = {}
        self.restored = runpy.run_path(str(HERE / "claude-traffic"), run_name="claude_traffic")["restored"]

    def config(self):
        return json.loads(self.accounts_file.read_text())

    def fetch(self, alias, path):
        hit = self.cache.get((alias, path))
        if hit and time.time() - hit[0] < CACHE_S:
            return hit[1]
        request = urllib.request.Request(
            "https://api.anthropic.com" + path,
            headers={"Authorization": f"Bearer proxy-account-{alias}", "anthropic-beta": "oauth-2025-04-20"},
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                value = json.load(response)
        except Exception as error:  # noqa: BLE001 - shown on that account's card
            value = {"error": str(error)[:200]}
        self.cache[(alias, path)] = (time.time(), value)
        return value

    def last_reply(self, alias, cfg):
        db = self.database(cfg)
        row = db.execute(
            "SELECT started, response_headers FROM flows WHERE served_by LIKE ? AND status = 200 "
            "AND response_headers LIKE '%ratelimit-unified%' ORDER BY started DESC LIMIT 1",
            (alias + "/%",),
        ).fetchone() if db else None
        return (reply_usage(json.loads(row[1])), row[0]) if row else ({}, None)

    def account(self, alias, slot, cfg):
        usage = self.fetch(alias, "/api/oauth/usage")
        profile = self.fetch(alias, "/api/oauth/profile")
        organization = profile.get("organization") or {}
        error = usage.get("error") or profile.get("error")
        windows = {
            w: {"utilization": usage[w].get("utilization"), "resets_at": usage[w].get("resets_at")}
            for w in WINDOWS
            if isinstance(usage.get(w), dict) and usage[w].get("utilization") is not None
        }
        token_only = inference_only(error)
        usage_at = None
        if not windows:
            windows, usage_at = self.last_reply(alias, cfg)
        return {
            "alias": alias,
            "slot": slot,
            "email": (profile.get("account") or {}).get("email"),
            "plan": (organization.get("organization_type") or "").removeprefix("claude_") or None,
            "usage": windows,
            "usage_at": usage_at,
            "token_only": token_only,
            "error": None if token_only else error,
            "disabled": alias in cfg.get("disabled", []),
        }

    def database(self, cfg):
        path = cfg.get("traffic_db")
        path = Path(path).expanduser() if path else None
        if not path or not path.exists():
            return None
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def state(self):
        cfg = self.config()
        with ThreadPoolExecutor(8) as pool:
            accounts = list(pool.map(lambda item: self.account(*item, cfg), cfg["accounts"].items()))
        db_path = Path(cfg["traffic_db"]).expanduser() if cfg.get("traffic_db") else None
        try:
            socket.create_connection((self.proxy.hostname, self.proxy.port), 1).close()
            proxy_up = True
        except OSError:
            proxy_up = False
        return {
            "accounts": accounts,
            "main": cfg.get("main"),
            "balance": cfg.get("balance", {"strategy": "session"}),
            "strategies": STRATEGIES,
            "traffic_days": cfg.get("traffic_days"),
            "renamed": cfg.get("renamed", {}),
            "proxy": {"address": f"{self.proxy.hostname}:{self.proxy.port}", "up": proxy_up},
            "db": {
                "path": str(db_path) if db_path else None,
                "bytes": sum(p.stat().st_size for p in db_path.parent.glob(db_path.name + "*")) if db_path and db_path.exists() else 0,
                "disk_free": shutil.disk_usage(db_path.parent if db_path else Path.home()).free,
            },
        }

    def traffic(self, query):
        db = self.database(self.config())
        if db is None:
            return {"rows": []}
        where, values = ["1=1"], []
        if query.get("inference"):
            where.append("model IS NOT NULL")
        for text in query.get("q", "").split():
            where.append("(client_host || ' ' || coalesce(client_user,'') || ' ' || coalesce(cwd,'') || ' ' || "
                         "coalesce(launch_cwd,'') || ' ' || coalesce(account,'') || ' ' || coalesce(served_by,'') || ' ' || "
                         "coalesce(model,'') || ' ' || url || ' ' || coalesce(status,'')) LIKE ?")
            values.append(f"%{text}%")
        limit = min(int(query.get("limit", 200)), 1000)
        rows = db.execute(f"SELECT {COLUMNS} FROM flows WHERE {' AND '.join(where)} ORDER BY started DESC LIMIT ?",
                          [*values, limit]).fetchall()
        names = COLUMNS.split(", ")
        return {"rows": [dict(zip(names, row), alias=alias_of(row[7])) for row in rows]}

    def stats(self, hours):
        db = self.database(self.config())
        if db is None:
            return {"hours": hours, "total": 0, "by_account": {}, "by_host": {}, "by_model": {}, "errors": 0, "series": {}}
        since = time.time() - hours * 3600
        rows = db.execute("SELECT started, account, served_by, coalesce(client_host, client_ip), model, status FROM flows "
                          "WHERE started > ? AND model IS NOT NULL", (since,)).fetchall()
        result = {"hours": hours, "total": len(rows), "by_account": {}, "by_host": {}, "by_model": {}, "errors": 0, "series": {}}
        bucket = max(hours * 3600 // 24, 3600)
        for started, account, served_by, host, model, status in rows:
            who = (served_by or "").split("/")[0] or alias_of(account) or "unknown"
            for key, value in (("by_account", who), ("by_host", host or "unknown"), ("by_model", model)):
                result[key][value] = result[key].get(value, 0) + 1
            result["errors"] += bool(status and status >= 400)
            series = result["series"].setdefault(who, [0] * 24)
            series[min(int((started - since) // bucket), 23)] += 1
        return result

    def flow(self, flow_id):
        db = self.database(self.config())
        if db is None:
            return None
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()
        if row is None:
            return None
        out = {}
        for key in row.keys():
            value = row[key]
            if key.endswith("_body"):
                value = zlib.decompress(value).decode(errors="replace") if value else ""
                if key == "request_body" and '"$blob"' in value:
                    value = json.dumps(self.restored(db, json.loads(value)), ensure_ascii=False, indent=1)
                value = value[:400_000]
            out[key] = value
        return out

    def update(self, change):
        cfg = self.config()
        aliases = list(cfg["accounts"])
        if "order" in change:
            if sorted(change["order"]) != sorted(aliases):
                raise ValueError("order must list every account once")
            cfg["accounts"] = {a: cfg["accounts"][a] for a in change["order"]}
        if "balance" in change:
            balance = change["balance"]
            if balance.get("strategy") not in STRATEGIES:
                raise ValueError("unknown strategy")
            if balance["strategy"] == "prefer" and balance.get("prefer") not in aliases:
                raise ValueError("prefer must name an account")
            cfg["balance"] = {k: v for k, v in balance.items() if k in ("strategy", "prefer")}
        if "disabled" in change:
            if not set(change["disabled"]) <= set(aliases):
                raise ValueError("disabled must name accounts")
            cfg["disabled"] = sorted(change["disabled"])
        if "traffic_days" in change:
            days = change["traffic_days"]
            if days is not None and (not isinstance(days, int) or days < 1):
                raise ValueError("traffic_days must be a positive whole number or empty")
            cfg["traffic_days"] = days
        shutil.copy2(self.accounts_file, self.accounts_file.with_name(self.accounts_file.name + ".before-ui"))
        tmp = self.accounts_file.with_suffix(".ui-tmp")
        tmp.write_text(json.dumps(cfg, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(self.accounts_file)
        return self.state()


def handler(dashboard):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("content-type", kind)
            self.send_header("cache-control", "no-store")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/":
                    return self.reply(200, (HERE / "account_ui.html").read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/state":
                    return self.reply(200, dashboard.state())
                if url.path == "/api/traffic":
                    return self.reply(200, dashboard.traffic(query))
                if url.path == "/api/stats":
                    return self.reply(200, dashboard.stats(int(query.get("hours", 24))))
                if url.path.startswith("/api/flow/"):
                    flow = dashboard.flow(url.path.removeprefix("/api/flow/"))
                    return self.reply(200 if flow else 404, flow or {"error": "no such flow"})
                return self.reply(404, {"error": "not found"})
            except (OSError, ValueError, sqlite3.Error) as error:
                return self.reply(500, {"error": str(error)})

        def do_POST(self):
            if urlsplit(self.path).path != "/api/config":
                return self.reply(404, {"error": "not found"})
            try:
                change = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                return self.reply(200, dashboard.update(change))
            except (ValueError, KeyError, TypeError) as error:
                return self.reply(400, {"error": str(error)})

        def log_message(self, *_):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--accounts-file", type=Path, required=True, help="the --serve proxy's accounts file")
    parser.add_argument("--proxy", required=True, help="the --serve proxy, http://HOST:PORT")
    parser.add_argument("--proxy-ca", type=Path, required=True, help="the --serve proxy's CA certificate")
    args = parser.parse_args()
    dashboard = Dashboard(args.accounts_file.expanduser(), args.proxy, args.proxy_ca.expanduser())
    server = ThreadingHTTPServer((args.listen_host, args.port), handler(dashboard))
    print(f"cc-proxy dashboard on http://{args.listen_host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
