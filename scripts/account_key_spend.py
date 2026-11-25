"""Month-to-date spend of a Console API key, from the traffic database and official prices.

Prices live only in `anthropic_prices.json` (the router reads the same file to estimate a request).
Spend is what the proxy sent through the key; anything else using the same key is not seen here.
"""

import calendar
import json
import re
import runpy
import time
from pathlib import Path

PRICES = json.loads(Path(__file__).with_name("anthropic_prices.json").read_text())["models"]
reply_tokens = runpy.run_path(str(Path(__file__).with_name("account_providers.py")))["reply_tokens"]
LONG_PROMPT = 100_000


def price(model, prompt_tokens=0):
    """USD per MTok for a price id, or that id with a `-YYYYMMDD` snapshot suffix; None if unknown."""
    rates = PRICES.get(re.sub(r"-\d{8}$", "", model))
    if rates is None:
        return None
    return rates.get("over_100k", rates) if prompt_tokens > LONG_PROMPT else rates


def reply_cost(model, tokens):
    """USD of one reply; `tokens` as `reply_tokens` returns them. An unknown model costs the dearest known price."""
    write = tokens.get("cache_write", 0)
    write_1h = tokens.get("cache_write_1h", 0)
    uncached = tokens["input"] - write
    rates = price(model, tokens["input"] + tokens["cache_read"])
    if rates is None:
        rates = max(PRICES.values(), key=lambda r: r["output"])
    return (uncached * rates["input"] + (write - write_1h) * rates["cache_write_5m"]
            + write_1h * rates["cache_write_1h"] + tokens["cache_read"] * rates["cache_read"]
            + tokens["output"] * rates["output"]) / 1e6


def month_start(now=None):
    """Unix seconds of the 1st of this month, 00:00 UTC, when Console credit resets."""
    day = time.gmtime(time.time() if now is None else now)
    return calendar.timegm((day.tm_year, day.tm_mon, 1, 0, 0, 0))


def month_spend(db, alias, now=None):
    """USD the proxy spent through `alias` since the 1st of the month (UTC). Rows recorded before the cache
    write columns existed are priced from their stored reply."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(flows)")}
    write = "cache_write_tokens" if "cache_write_tokens" in columns else "NULL"
    write_1h = "cache_write_1h_tokens" if "cache_write_1h_tokens" in columns else "NULL"
    rows = db.execute(
        f"SELECT served_by, input_tokens, cache_read_tokens, output_tokens, {write}, {write_1h}, "
        f"CASE WHEN {write} IS NULL THEN response_body END FROM flows WHERE started >= ? AND status = 200 "
        "AND served_by IS NOT NULL AND served_by LIKE ? ESCAPE '\\' AND coalesce(url, '') NOT LIKE '%/v1/messages/count_tokens%'",
        (month_start(now), alias.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"),
    ).fetchall()
    total = 0.0
    for served_by, inp, read, out, cache_write, cache_write_1h, body in rows:
        tokens = reply_tokens(body) if cache_write is None else {
            "input": inp or 0, "cache_read": read or 0, "output": out or 0,
            "cache_write": cache_write, "cache_write_1h": cache_write_1h or 0}
        if tokens:
            total += reply_cost(served_by.split("/", 1)[1], tokens)
    return total
