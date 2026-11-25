"""Dashboard insights from the traffic database: load, speed and health, overall, per account and per model.

Everything is derived from rows account_traffic.py already writes (`flows`) plus its one-row `live`
snapshot of open connections and in-flight model calls. A metric with no samples is None (unknown),
never zero: rows recorded before `first_token` existed have no TTFT, and a stale snapshot has no live counts.
"""

import json
import math
import time

ACTIVE_S = 15 * 60  # a session or machine with a model call this recent counts as active
CURRENT_S = 5 * 60  # "current" TTFT and tokens/s
LIVE_STALE_S = 30  # the writer refreshes `live` every 5 s; older means the recorder is not running
MIN_GENERATION_S = 0.05  # shorter streams give meaningless tokens/s


def account_of(served_by, placeholder):
    """Who answered: the served-by prefix (a Claude login or a backend), else the session's own alias."""
    return (served_by or "").split("/")[0] or (placeholder or "").removeprefix("proxy-account-").split("+")[0] or "unknown"


def model_of(served_by, requested):
    return served_by.split("/", 1)[1] if served_by and "/" in served_by else (requested or "unknown")


def base_model(model):
    return (model or "").removesuffix("[1m]")


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)  # nearest rank
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def live_snapshot(db, now):
    """The recorder's latest connection and in-flight snapshot, or None when missing or stale."""
    try:
        row = db.execute("SELECT at, body FROM live WHERE name = 'now'").fetchone()
    except Exception:  # noqa: BLE001 - a database from before `live` existed
        return None
    if not row or now - row[0] > LIVE_STALE_S:
        return None
    body = json.loads(row[1])
    return {"at": row[0], "connections": body.get("connections", []), "inflight": body.get("inflight", []),
            "dropped": body.get("dropped")}


def empty():
    return {"requests": 0, "errors": 0, "fallbacks": 0, "statuses": {}, "sessions": set(), "active_sessions": set(),
            "machines": set(), "active_machines": set(), "ttft": [], "ttft_now": [], "speed": [], "speed_now": [],
            "latency": [], "tokens": {"input": 0, "cache_read": 0, "output": 0}, "tokens_reported": 0,
            "output_now": 0, "inflight": 0}


def add(group, row, now):
    started, finished, first_token, session, host, status, model, served, tokens = row
    group["requests"] += 1
    key = str(status) if status is not None else "no reply"
    group["statuses"][key] = group["statuses"].get(key, 0) + 1
    group["errors"] += status is None or status >= 400
    group["fallbacks"] += bool(served) and base_model(model) != base_model(model_of(served, model))
    recent = now - started <= ACTIVE_S
    if session:
        group["sessions"].add(session)
        if recent:
            group["active_sessions"].add(session)
    group["machines"].add(host)
    if recent:
        group["active_machines"].add(host)
    if finished:
        group["latency"].append(finished - started)
    current = now - started <= CURRENT_S
    if first_token:
        group["ttft"].append(first_token - started)
        if current:
            group["ttft_now"].append(first_token - started)
    if tokens[0] is not None:
        group["tokens_reported"] += 1
        for name, value in zip(("input", "cache_read", "output"), tokens):
            group["tokens"][name] += value or 0
        if current:
            group["output_now"] += tokens[2] or 0
        if first_token and finished and tokens[2] and finished - first_token >= MIN_GENERATION_S:
            rate = tokens[2] / (finished - first_token)
            group["speed"].append(rate)
            if current:
                group["speed_now"].append(rate)


def summary(group, minutes):
    tokens, reported = group["tokens"], group["tokens_reported"]
    prompt = tokens["input"] + tokens["cache_read"]
    requests = group["requests"]
    return {
        "requests": requests,
        "per_minute": requests / minutes,
        "errors": group["errors"],
        "error_rate": group["errors"] / requests if requests else None,
        "fallbacks": group["fallbacks"],
        "fallback_rate": group["fallbacks"] / requests if requests else None,
        "statuses": group["statuses"],
        "sessions": len(group["sessions"]),
        "active_sessions": len(group["active_sessions"]),
        "machines": len(group["machines"]),
        "active_machines": len(group["active_machines"]),
        "inflight": group["inflight"],
        "ttft": {"now": percentile(group["ttft_now"], 0.5), "p50": percentile(group["ttft"], 0.5),
                 "p95": percentile(group["ttft"], 0.95), "samples": len(group["ttft"])},
        "speed": {"now": percentile(group["speed_now"], 0.5), "p50": percentile(group["speed"], 0.5),
                  "p95": percentile(group["speed"], 0.95), "samples": len(group["speed"])},
        "latency": {"p50": percentile(group["latency"], 0.5), "p95": percentile(group["latency"], 0.95)},
        "throughput_now": group["output_now"] / CURRENT_S if reported else None,
        "tokens": tokens if reported else None,
        "tokens_reported": reported,
        "cache_hit": tokens["cache_read"] / prompt if reported and prompt else None,
    }


def insights(db, hours, now=None):
    """Totals and per-account / per-model breakdowns of model calls over the last `hours`."""
    now = time.time() if now is None else now
    columns = {row[1] for row in db.execute("PRAGMA table_info(flows)")}
    first_token = "first_token" if "first_token" in columns else "NULL"
    tokens = ("input_tokens, cache_read_tokens, output_tokens"
              if {"input_tokens", "cache_read_tokens", "output_tokens"} <= columns else "NULL, NULL, NULL")
    rows = db.execute(
        f"SELECT started, finished, {first_token}, session_id, coalesce(client_host, client_ip, 'unknown'), "
        f"status, model, served_by, account, {tokens} FROM flows WHERE started > ? AND model IS NOT NULL "
        "AND coalesce(url, '') NOT LIKE '%/v1/messages/count_tokens%'", (now - hours * 3600,)).fetchall()
    total, accounts, models = empty(), {}, {}
    for started, finished, first, session, host, status, model, served, placeholder, *counts in rows:
        row = (started, finished, first, session, host, status, model, served, counts)
        for group in (total, accounts.setdefault(account_of(served, placeholder), empty()),
                      models.setdefault(model_of(served, model), empty())):
            add(group, row, now)
    live = live_snapshot(db, now)
    if live:
        for call in live["inflight"]:
            served = call.get("served_by")
            total["inflight"] += 1
            accounts.setdefault(account_of(served, call.get("account")), empty())["inflight"] += 1
            if served:
                models.setdefault(model_of(served, None), empty())["inflight"] += 1
    minutes = hours * 60
    named = lambda groups, key: sorted(({key: name, **summary(g, minutes)} for name, g in groups.items()),
                                       key=lambda e: (e["inflight"], e["requests"]), reverse=True)
    return {
        "hours": hours, "traffic_db": True, "timings_recorded": "first_token" in columns,
        "live": None if live is None else {
            "at": live["at"], "connections": len(live["connections"]),
            "client_ips": len({ip for ip in live["connections"] if ip}),
            "inflight": len(live["inflight"]), "dropped": live["dropped"]},
        "total": summary(total, minutes),
        "by_account": named(accounts, "account"),
        "by_model": named(models, "model"),
    }
