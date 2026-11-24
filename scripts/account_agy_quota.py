"""Antigravity (`agy`) model quota, read the way the CLI's `/usage` panel reads it.

agy has no headless usage command. Its `/usage` panel shows per model group a five-hour and a
weekly limit from Cloud Code's `retrieveUserQuotaSummary`; groups the summary omits (Gemini on a
Starter account) only appear per model in `retrieveUserQuota`. Both are called on
daily-cloudcode-pa, the host agy itself uses, with the access token agy stores in
`~/.gemini/antigravity-cli/antigravity-oauth-token` (its file store, used when no keyring is
reachable). Read fresh, never refreshed or written: agy renews it on its next run, so an expired
token reports unknown. Read-only; costs no inference.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

HOST = "https://daily-cloudcode-pa.googleapis.com/v1internal:"
TIMEOUT = 10
SOURCE = "Antigravity Cloud Code quota (agy login)"


def token_path():
    return os.path.join(os.path.expanduser("~"), ".gemini/antigravity-cli/antigravity-oauth-token")


def read_token(path=None, now=None):
    """(access token, expired) or None when agy has no file login here."""
    try:
        with open(path or token_path()) as f:
            token = json.load(f).get("token") or {}
        access, expiry = token.get("access_token"), token.get("expiry")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(access, str) or not access.strip():
        return None
    try:
        # Go writes nanoseconds, more digits than fromisoformat takes.
        expires = datetime.fromisoformat(re.sub(r"\.\d+", "", expiry)).timestamp()
    except (TypeError, ValueError):
        expires = None
    return access.strip(), expires is not None and expires <= (time.time() if now is None else now)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def rpc(token, method):
    request = urllib.request.Request(
        HOST + method, data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "antigravity/cli"},
    )
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=TIMEOUT) as response:
        return json.loads(response.read() or b"{}")


def _used(fraction):
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1:
        return None
    return round((1 - fraction) * 100, 1)


def parse(summary, per_model=None, now=None):
    """The two replies -> a card quota of percentage windows. A bucket without a readable
    remaining fraction is left out rather than shown as zero."""
    windows = []
    groups = (summary or {}).get("groups") or []
    for group in groups:
        name = group.get("displayName") or "Models"
        for bucket in group.get("buckets") or []:
            used = _used(bucket.get("remainingFraction", (bucket.get("remaining") or {}).get("remainingFraction")))
            if used is None:
                continue
            window = {"5h": "5-hour", "weekly": "weekly"}.get(bucket.get("window"), bucket.get("displayName") or "limit")
            windows.append({"label": f"{name} · {window}", "used": used, "limit": 100, "unit": "%",
                            "resets_at": bucket.get("resetTime")})
    if not any("gemini" in (group.get("displayName") or "").lower() for group in groups):
        gemini = [b for b in (per_model or {}).get("buckets") or []
                  if str(b.get("modelId", "")).startswith("gemini-") and b.get("resetTime")
                  and _used(b.get("remainingFraction")) is not None]
        if gemini:
            tightest = min(gemini, key=lambda b: b["remainingFraction"])
            windows.append({"label": "Gemini models", "used": _used(tightest["remainingFraction"]), "limit": 100,
                            "unit": "%", "resets_at": tightest["resetTime"]})
    return {"source": SOURCE, "at": time.time() if now is None else now, "windows": windows,
            "note": "one quota per agy login, shared by every client of it; Gemini is the tightest per-model "
                    "bucket when the summary has no Gemini group"}


def agy_quota(now=None, login=None, call=rpc):
    """Card quota for the signed-in agy. None without an agy login (unknown, not zero);
    {"error": ...} when the token expired or Cloud Code refused or could not be reached."""
    login = read_token(now=now) if login is None else login
    if not login:
        return None
    token, expired = login
    if expired:
        return {"error": "agy access token expired; agy renews it on its next run"}
    method = "retrieveUserQuotaSummary"
    try:
        summary = call(token, method)
        method = "retrieveUserQuota"
        try:
            per_model = call(token, method)
        except Exception:  # noqa: BLE001 - the per-model reply only fills a group the summary lacks
            per_model = None
        if not isinstance(summary, dict):
            raise TypeError("invalid summary")
        return parse(summary, per_model if isinstance(per_model, dict) else None, now)
    except urllib.error.HTTPError as error:
        return {"error": f"{method} HTTP {error.code}"}
    except Exception as error:  # noqa: BLE001 - vendor failures are isolated and credentials never enter error text
        return {"error": f"{method}: {type(error).__name__}"}


if __name__ == "__main__":
    print(json.dumps(agy_quota(), indent=2))
