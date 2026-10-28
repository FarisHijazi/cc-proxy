"""Recorded usage of the non-Claude backends, for the dashboard's provider cards.

Only Cursor's quota is readable (account_cursor_quota.py, cursor-agent's own login). The router
consumes Codex's `codex.rate_limits` event without recording it, its translated replies carry no
rate-limit headers, and gemini-web-api, Kimi and Grok report none. So every card shows what the
traffic database recorded (requests answered, reported tokens, last activity) over an explicit
window, and a card without a vendor snapshot says its quota is unknown instead of drawing a meter.
"""

import json
import time
import zlib
from datetime import UTC, datetime

WINDOW_HOURS = 24
# Bodies are decoded only for these providers' rows in the window, newest first, at most this many.
ROW_LIMIT = 2000
# Claude accounts have their own cards; served_by for them is "<alias>/<model>".
PROVIDERS = ["codex", "cursor-cli", "cursor", "gemini", "kimi", "grok"]
# Rows recorded before the router tagged direct (non-fallback) replies have no served_by, so their
# backend comes from the requested id, in src/registry.rs provider_for_model's order: Claude ids
# first, cursor-cli before the cursor prefixes, exact ids, then the gpt-/gemini- families.
ROUTES = [
    ("anthropic", ["haiku", "sonnet", "opus", "fable"], ["claude-"]),
    ("cursor-cli", ["cursor-cli", "cursor-cli-ask", "cursor-cli-plan", "cursor-cli-agent"],
     ["cursor-cli-agent:", "cursor-cli-plan:", "cursor-cli-ask:", "cursor-cli:"]),
    ("cursor", ["cursor", "cursor-agent", "cursor-composer", "cursor-composer-fast", "cursor-plan", "cursor-ask",
                "composer-2.5", "composer-2.5-fast"], ["cursor:", "cursor-plan:", "cursor-ask:"]),
    ("kimi", ["kimi-for-coding", "kimi-k2.6", "kimi-k3", "k2.6", "k3"], []),
    ("grok", ["grok-composer-2.5-fast", "grok-4.5"], []),
    ("codex", [], ["gpt-"]),
    ("gemini", [], ["gemini-"]),
]


def backend_sql():
    """SQL for a row's backend: the router's served_by when it tagged one, else the requested id's
    route. Reads column `m`, the model id without the `[1m]` suffix the router also strips."""
    cases, params = [], []
    for name, exact, prefixes in ROUTES:
        tests = [f"m IN ({','.join('?' * len(exact))})"] if exact else []
        tests += ["substr(m, 1, ?) = ?"] * len(prefixes)
        cases.append(f"WHEN {' OR '.join(tests)} THEN ?")
        params += [*exact, *(x for prefix in prefixes for x in (len(prefix), prefix)), name]
    tagged = "nullif(substr(served_by, 1, instr(served_by, '/') - 1), '')"
    return f"coalesce({tagged}, CASE {' '.join(cases)} END)", params


def reply_tokens(body):
    """Token usage an Anthropic-shaped reply reported: one JSON message, or SSE whose
    message_start and later message_delta events each carry part of it. None if absent or unreadable."""
    if not body:
        return None
    try:
        text = zlib.decompress(body).decode(errors="replace")
    except (zlib.error, TypeError):
        return None
    events = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")] or [text]
    usage = {}
    for event in events:
        try:
            data = json.loads(event)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        found = data.get("usage") or (data.get("message") or {}).get("usage")
        if isinstance(found, dict):
            usage.update({k: v for k, v in found.items() if isinstance(v, int) and not isinstance(v, bool)})
    if not usage:
        return None
    return {
        "input": usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "output": usage.get("output_tokens", 0),
    }


def provider_usage(db, now=None, hours=WINDOW_HOURS, limit=ROW_LIMIT):
    """Per backend: model calls it answered in the last `hours`, with reported tokens.
    Token counting calls are not model calls and are left out.

    None when there is no traffic database: unknown, not zero."""
    if db is None:
        return None
    now = time.time() if now is None else now
    since = now - hours * 3600
    backend, backend_params = backend_sql()
    rows = (
        f"(SELECT *, {backend} AS backend FROM (SELECT *, CASE WHEN lower(model) LIKE '%[1m]' "
        "THEN substr(model, 1, length(model) - 4) ELSE model END AS m FROM flows "
        "WHERE started > ? AND model IS NOT NULL AND coalesce(url, '') NOT LIKE '%/v1/messages/count_tokens%'))"
    )
    where = f"backend IN ({','.join('?' * len(PROVIDERS))})"
    params = (*backend_params, since, *PROVIDERS)
    result = {
        name: {"requests": 0, "errors": 0, "last_at": None, "last_model": None, "last_status": None,
               "tokens": {"input": 0, "cache_read": 0, "output": 0}, "tokens_reported": 0}
        for name in PROVIDERS
    }
    # SQLite fills the bare columns from the row holding max(started): the latest call.
    for name, count, errors, last_at, model, status in db.execute(
        f"SELECT backend, count(*), sum(status >= 400), max(started), "
        f"coalesce(nullif(substr(served_by, instr(served_by, '/') + 1), ''), m), status FROM {rows} "
        f"WHERE {where} GROUP BY backend", params,
    ):
        result[name].update(requests=count, errors=errors or 0, last_at=last_at, last_model=model, last_status=status)
    decoded = db.execute(
        f"SELECT backend, response_body FROM {rows} WHERE {where} ORDER BY started DESC LIMIT ?", (*params, limit),
    ).fetchall()
    for name, body in decoded:
        tokens = reply_tokens(body)
        if tokens:
            result[name]["tokens_reported"] += 1
            for key, value in tokens.items():
                result[name]["tokens"][key] += value
    return {
        "source": "traffic database",
        "hours": hours,
        "since": since,
        "at": now,
        # Counts always cover the whole window; tokens only the newest `limit` replies when this is true.
        "tokens_capped": len(decoded) >= limit,
        "providers": result,
    }


def cursor_card_quota(quota):
    """account_cursor_quota's snapshot as a card's quota: None stays None (no CLI login, unknown)."""
    if not quota or quota.get("error"):
        return quota
    windows, notes = [], []
    included, on_demand = quota.get("included"), quota.get("on_demand")
    resets = datetime.fromtimestamp(quota["resets_at"], UTC).isoformat() if quota.get("resets_at") else None
    if included:
        # Cursor reports three readings that disagree: dollars of the included budget (its own "you've
        # used N%" message), and the API (named models, e.g. cursor-cli:<claude id>) and Auto buckets
        # that cursor-agent's /usage shows. Show all three rather than pick one.
        windows += [
            {"label": "Included", "used": included["used_cents"] / 100, "limit": included["limit_cents"] / 100,
             "unit": "USD", "resets_at": resets},
            {"label": "API", "used": included["api_percent"], "limit": 100, "unit": "%", "resets_at": None},
            {"label": "Auto", "used": included["auto_percent"], "limit": 100, "unit": "%", "resets_at": None},
        ]
        notes.append("API = named models such as cursor-cli Claude/GPT ids; Auto = Auto/Composer; "
                     f"cursor-agent /usage total {included['total_percent']:.0f}%")
    if on_demand and on_demand["enabled"]:
        limit = on_demand["limit_cents"]
        windows.append({"label": "On-demand", "used": on_demand["used_cents"] / 100,
                        "limit": limit / 100 if limit else None, "unit": "USD", "resets_at": resets})
    elif on_demand:
        notes.append("on-demand usage off: stops when included usage runs out")
    plan = f"{quota['plan']} plan, " if quota.get("plan") else ""
    return {"source": quota["source"], "at": quota["at"], "windows": windows,
            "note": f"{plan}one budget shared by every client of this Cursor login. " + "; ".join(notes)}
