"""Cursor included-usage quota, read the way `cursor-agent`'s own `/usage` command reads it.

`cursor-agent` (2026.10.22) has no headless usage subcommand: its interactive `/usage` calls
`aiserver.v1.DashboardService` GetCurrentPeriodUsage / GetPlanInfo / GetHardLimit on
api2.cursor.sh with the CLI's signed-in bearer (`src/usage/usage-data.ts` in its bundle). This
makes the same three Connect-JSON calls with that same login, read fresh from where the CLI
keeps it (the macOS Keychain, else `$XDG_CONFIG_HOME/cursor/auth.json`, as
`cursor::auth::cursor_agent_token` reads it) and never written or refreshed here. Read-only; costs no inference.

Money is in US cents; times are epoch seconds. One budget per Cursor account: the IDE, the CLI,
the proxy's `cursor` backend and `cursor-cli` all draw on it. "auto" is Cursor's Auto/Composer
bucket (`auto_models`); "api" is named-model selection (e.g. a Claude/GPT id), not API keys.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ENDPOINT = "https://api2.cursor.sh/aiserver.v1.DashboardService/"
TIMEOUT = 10  # the CLI's own timeoutMs


def auth_path():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "cursor/auth.json")


def keychain_token():
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", "cursor-access-token", "-a", "cursor-user", "-w"],
                             capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def read_token(path=None):
    if path is None and sys.platform == "darwin":
        return keychain_token()
    try:
        with open(path or auth_path()) as f:
            token = json.load(f).get("accessToken")
        return (token.strip() or None) if isinstance(token, str) else None
    except (OSError, ValueError, AttributeError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def connect_call(token, rpc):
    req = urllib.request.Request(
        ENDPOINT + rpc, data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Connect-Protocol-Version": "1"},
    )
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _secs(ms):
    ms = int(ms or 0)
    return ms / 1000 if ms > 0 else None


def parse(usage, plan=None, hard=None, now=None):
    """The three RPC replies -> the dashboard's quota dict. `plan`/`hard` may be None (the CLI
    treats those calls as optional too)."""
    plan_info = (plan or {}).get("planInfo") or {}
    pu = usage.get("planUsage")
    resets = _secs(usage.get("billingCycleEnd")) or _secs(plan_info.get("billingCycleEnd"))
    out = {
        "source": "cursor DashboardService (cursor-agent login)",
        "at": time.time() if now is None else now,
        "plan": plan_info.get("planName") or None,
        "period_start": _secs(usage.get("billingCycleStart")),
        "resets_at": resets,
        "included": None,
        "on_demand": None,
        "message": usage.get("displayMessage") or None,
    }
    if pu is not None:
        used, limit = pu.get("includedSpend", 0), pu.get("limit", 0)
        out["included"] = {
            "used_cents": used,
            "limit_cents": limit,
            "remaining_cents": pu.get("remaining", max(limit - used, 0)),
            "spend_percent": used / limit * 100 if limit > 0 else None,
            # The CLI's /usage headline: totalPercentUsed when sent, else spend/limit.
            "total_percent": pu.get("totalPercentUsed", used / limit * 100 if limit > 0 else 0),
            "auto_percent": pu.get("autoPercentUsed", 0),
            "api_percent": pu.get("apiPercentUsed", 0),
        }
    sl = usage.get("spendLimitUsage") or {}
    od_used = sl.get("individualUsed", 0)
    if sl.get("individualLimit") is not None:
        limit = sl["individualLimit"]
        out["on_demand"] = {"enabled": limit > 0, "used_cents": od_used, "limit_cents": limit if limit > 0 else None}
    elif hard is not None:
        # getHardLimit reports whole dollars; 2147483647 means unlimited.
        cap = hard.get("hardLimit", 0)
        enabled = not hard.get("noUsageBasedAllowed") and cap > 0
        out["on_demand"] = {"enabled": enabled, "used_cents": od_used,
                            "limit_cents": cap * 100 if enabled and cap < 2147483647 else None}
    return out


def cursor_quota(now=None, token=None, call=connect_call):
    """Live quota for the signed-in cursor-agent. None when there is no CLI login (unknown, not
    zero); {"error": ...} when Cursor refused or could not be reached."""
    token = read_token() if token is None else token
    if not isinstance(token, str) or not token.strip():
        return None
    token = token.strip()
    try:
        usage = call(token, "GetCurrentPeriodUsage")
        if not isinstance(usage, dict):
            raise ValueError("invalid usage object")
        optional = {}
        for rpc in ("GetPlanInfo", "GetHardLimit"):
            try:
                value = call(token, rpc)
                optional[rpc] = value if isinstance(value, dict) else None
            except Exception:  # noqa: BLE001 - optional vendor data must not break the dashboard
                optional[rpc] = None
        return parse(usage, optional["GetPlanInfo"], optional["GetHardLimit"], now)
    except urllib.error.HTTPError as e:
        return {"error": f"GetCurrentPeriodUsage HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 - vendor failures are isolated and credentials never enter error text
        return {"error": f"GetCurrentPeriodUsage: {type(e).__name__}"}


if __name__ == "__main__":
    print(json.dumps(cursor_quota(), indent=2))
