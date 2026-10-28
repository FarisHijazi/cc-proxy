"""ChatGPT plan limits for the Codex login, read the way the Codex CLI's `/status` reads them.

`GET chatgpt.com/backend-api/wham/usage` with the access token from the Codex CLI's
`$CODEX_HOME/auth.json` (the file the router's codex backend also reads). Read fresh, never
written or refreshed here: the router owns refresh. Read-only; costs no inference.

Returns a dashboard card quota (`windows` of used/limit percentages with reset times), None
when there is no Codex login (unknown, not zero), or {"error": ...} with no vendor text.
"""

import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

ENDPOINT = "https://chatgpt.com/backend-api/wham/usage"
TIMEOUT = 10


def auth_path():
    return os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "auth.json")


def read_login(path=None):
    """(access token, ChatGPT account id) or None."""
    try:
        with open(path or auth_path()) as f:
            tokens = json.load(f).get("tokens") or {}
        token, account = tokens.get("access_token"), tokens.get("account_id")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(token, str) or not token.strip():
        return None
    return token.strip(), account.strip() if isinstance(account, str) else ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(login):
    token, account = login
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "codex_cli_rs"}
    if account:
        headers["ChatGPT-Account-Id"] = account
    req = urllib.request.Request(ENDPOINT, headers=headers)
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _span(seconds):
    if not seconds:
        return "Window"
    hours = seconds / 3600
    return "Weekly" if round(hours) == 168 else f"{hours:g}h" if hours < 48 else f"{hours / 24:g}d"


def _windows(limit, prefix=""):
    out = []
    for key in ("primary_window", "secondary_window"):
        w = (limit or {}).get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        resets = w.get("reset_at")
        out.append({"label": prefix + _span(w.get("limit_window_seconds")), "used": float(w["used_percent"]),
                    "limit": 100, "unit": "%",
                    "resets_at": datetime.fromtimestamp(resets, UTC).isoformat() if resets else None})
    return out


def parse(usage, now=None):
    windows = _windows(usage.get("rate_limit"))
    for extra in usage.get("additional_rate_limits") or []:
        if isinstance(extra, dict):
            windows += _windows(extra.get("rate_limit"), f"{extra.get('limit_name') or 'Extra'} ")
    windows += _windows(usage.get("code_review_rate_limit"), "Code review ")
    notes = []
    if (usage.get("rate_limit") or {}).get("limit_reached"):
        notes.append("limit reached")
    credits = usage.get("credits") or {}
    if credits.get("unlimited"):
        notes.append("unlimited credits")
    elif credits.get("has_credits"):
        notes.append(f"credit balance {credits.get('balance')}")
    plan = usage.get("plan_type")
    note = ", ".join(filter(None, [f"{plan} plan" if plan else None, *notes]))
    return {"source": "ChatGPT usage API (Codex CLI login)", "at": time.time() if now is None else now,
            "windows": windows, "note": f"{note}; one budget shared by Codex CLI and the router" if note
            else "one budget shared by Codex CLI and the router"}


def codex_quota(now=None, login=None, call=fetch):
    login = read_login() if login is None else login
    if not login:
        return None
    try:
        usage = call(login)
        if not isinstance(usage, dict):
            raise ValueError("invalid usage object")
        return parse(usage, now)
    except urllib.error.HTTPError as e:
        return {"error": f"Codex usage HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 - vendor failures are isolated and credentials never enter error text
        return {"error": f"Codex usage: {type(e).__name__}"}


if __name__ == "__main__":
    print(json.dumps(codex_quota(), indent=2))
