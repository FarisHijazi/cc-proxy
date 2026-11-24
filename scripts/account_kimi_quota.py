"""Read Moonshot monetary credit with the router's Moonshot API key, else its Kimi auth store.

The caller supplies the router's current endpoint from its model catalogue metadata.
Coding quota has no verified API here. Never refresh, write auth, or infer token limits.
"""

import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BALANCE_BASE = "https://api.moonshot.ai/v1"
TIMEOUT = 10


def read_login():
    root = Path(os.environ.get("CCP_CONFIG_DIR") or
                Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "claude-code-proxy")
    try:
        key = (root / "kimi/api_key").read_text().strip()
    except OSError:
        key = ""
    if key:
        return key
    paths = [root / "kimi/auth.json", Path.home() / ".config/claude-code-proxy/kimi/auth.json"]
    for path in dict.fromkeys(paths):
        try:
            token = json.loads(path.read_text()).get("access")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(token, endpoint):
    request = urllib.request.Request(endpoint, headers={"Authorization": f"Bearer {token}",
                                                     "Accept": "application/json"})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=TIMEOUT) as response:
        return json.load(response)


def kimi_quota(base_url=None, now=None, login=None, call=fetch):
    snapshot = {"source": "Moonshot balance API", "at": time.time() if now is None else now,
                "state": "unavailable", "windows": [],
                "note": "Balance unavailable; token quota is not reported"}
    if not isinstance(base_url, str) or base_url.rstrip("/") != BALANCE_BASE:
        snapshot["note"] = "Quota unavailable for this Kimi endpoint; no verified Coding quota API"
        return snapshot
    token = read_login() if login is None else login
    if not token:
        snapshot["note"] = "No readable Kimi access credential on the dashboard host"
        return snapshot
    try:
        reply = call(token, BALANCE_BASE + "/users/me/balance")
        if not isinstance(reply, dict) or reply.get("code") != 0 or reply.get("status") is not True:
            return snapshot
        data = reply.get("data")
        if not isinstance(data, dict):
            return snapshot
        balances = {name: data.get(name + "_balance") for name in ("available", "voucher", "cash")}
        valid = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if not valid(balances["available"]):
            return snapshot
        snapshot.update(state="available", currency="credits",
                        balances={name: value for name, value in balances.items() if valid(value)},
                        note="Monetary credit, not a token allowance or percentage quota")
    except urllib.error.HTTPError as error:
        snapshot.update(state="error", error=f"Kimi balance HTTP {error.code}")
    except Exception:  # noqa: BLE001 - no vendor text or credentials in optional dashboard data
        snapshot.update(state="error", error="Kimi balance request failed")
    return snapshot
