"""Cursor quota read helper: parsing of real DashboardService replies, and unknown vs error."""

import http.client
import json
import runpy
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

quota = runpy.run_path(str(Path(__file__).parents[1] / "scripts/account_cursor_quota.py"), run_name="account_cursor_quota")
NOW = 1_800_000_000.0

# Shapes captured from a live Pro account (2026-10-28), model list trimmed.
USAGE = {
    "billingCycleStart": "1792438389000", "billingCycleEnd": "1795116789000",
    "planUsage": {"totalSpend": 1825, "includedSpend": 1825, "remaining": 175, "limit": 2000,
                  "autoPercentUsed": 0.5133, "apiPercentUsed": 35.4222, "totalPercentUsed": 3.6868},
    "spendLimitUsage": {"limitType": "user"}, "enabled": True,
    "displayMessage": "You've used 91% of your included usage", "autoBucketModels": ["default"],
}
PLAN = {"planInfo": {"planName": "Pro", "billingCycleEnd": "1795116789000"}}
HARD = {"noUsageBasedAllowed": True}


def test_parses_live_pro_reply():
    q = quota["parse"](USAGE, PLAN, HARD, NOW)
    assert q["plan"] == "Pro"
    assert q["period_start"] == 1792438389.0 and q["resets_at"] == 1795116789.0
    inc = q["included"]
    assert (inc["used_cents"], inc["limit_cents"], inc["remaining_cents"]) == (1825, 2000, 175)
    assert inc["spend_percent"] == 91.25
    assert (inc["total_percent"], inc["auto_percent"], inc["api_percent"]) == (3.6868, 0.5133, 35.4222)
    assert q["on_demand"] == {"enabled": False, "used_cents": 0, "limit_cents": None}
    assert q["message"].startswith("You've used 91%")


def test_individual_spend_limit_wins_and_is_cents():
    usage = dict(USAGE, spendLimitUsage={"limitType": "user", "individualUsed": 250, "individualLimit": 5000})
    assert quota["parse"](usage, PLAN, HARD, NOW)["on_demand"] == {"enabled": True, "used_cents": 250, "limit_cents": 5000}


def test_hard_limit_is_dollars_and_max_int_is_unlimited():
    assert quota["parse"](USAGE, PLAN, {"hardLimit": 40}, NOW)["on_demand"]["limit_cents"] == 4000
    assert quota["parse"](USAGE, PLAN, {"hardLimit": 2147483647}, NOW)["on_demand"] == {
        "enabled": True, "used_cents": 0, "limit_cents": None}


def test_missing_plan_usage_and_cycle_fall_back():
    q = quota["parse"]({"billingCycleEnd": "0"}, PLAN, None, NOW)
    assert q["included"] is None and q["on_demand"] is None
    assert q["resets_at"] == 1795116789.0  # from GetPlanInfo, as the CLI does


def test_no_login_is_unknown_not_zero(tmp_path):
    assert quota["read_token"](str(tmp_path / "missing.json")) is None
    (tmp_path / "auth.json").write_text(json.dumps({"refreshToken": "toy"}))
    assert quota["read_token"](str(tmp_path / "auth.json")) is None


def test_live_call_uses_all_three_rpcs_and_optional_failures_are_tolerated():
    seen = []

    def call(token, rpc):
        seen.append((token, rpc))
        if rpc == "GetHardLimit":
            raise OSError("down")
        return {"GetCurrentPeriodUsage": USAGE, "GetPlanInfo": PLAN}[rpc]

    q = quota["cursor_quota"](NOW, token="toy", call=call)
    assert [r for _, r in seen] == ["GetCurrentPeriodUsage", "GetPlanInfo", "GetHardLimit"]
    assert q["included"]["used_cents"] == 1825 and q["on_demand"] is None


def test_usage_failure_is_an_error_not_a_meter():
    def call(token, rpc):
        raise urllib.error.HTTPError("u", 401, "no", {}, None)

    assert quota["cursor_quota"](NOW, token="toy", call=call) == {"error": "GetCurrentPeriodUsage HTTP 401"}


@pytest.mark.parametrize("failure", [
    http.client.IncompleteRead(b"truncated"),
    ValueError("Invalid header value b'Bearer toy-private-token\\n'"),
    RuntimeError("unexpected vendor failure"),
])
def test_vendor_failures_are_isolated_and_never_echo_credentials(failure):
    def call(token, rpc):
        raise failure

    result = quota["cursor_quota"](NOW, token="toy-private-token", call=call)
    assert result == {"error": f"GetCurrentPeriodUsage: {type(failure).__name__}"}
    assert "toy-private-token" not in json.dumps(result)


@pytest.mark.parametrize("value", [[], {"billingCycleEnd": "soon"}, {"planUsage": []}])
def test_malformed_vendor_payload_is_an_error_not_a_dashboard_failure(value):
    result = quota["cursor_quota"](NOW, token="toy", call=lambda token, rpc: value)
    assert "error" in result and "included" not in result


def test_optional_vendor_failures_do_not_hide_required_usage():
    def call(token, rpc):
        if rpc == "GetCurrentPeriodUsage":
            return USAGE
        raise http.client.IncompleteRead(b"truncated")

    result = quota["cursor_quota"](NOW, token="toy", call=call)
    assert result["included"]["used_cents"] == 1825
    assert result["plan"] is None


def test_token_whitespace_is_trimmed_without_changing_source(tmp_path):
    path = tmp_path / "auth.json"
    original = json.dumps({"accessToken": " toy-private-token\n"})
    path.write_text(original)
    assert quota["read_token"](path) == "toy-private-token"
    assert path.read_text() == original
    seen = []

    def call(token, rpc):
        seen.append(token)
        return USAGE if rpc == "GetCurrentPeriodUsage" else {}

    assert "error" not in quota["cursor_quota"](NOW, token=" toy-private-token\n", call=call)
    assert set(seen) == {"toy-private-token"}


def test_authenticated_requests_never_follow_redirects(monkeypatch):
    reached = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.end_headers()

        def do_GET(self):
            reached.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setitem(quota["connect_call"].__globals__, "ENDPOINT", f"http://127.0.0.1:{server.server_port}/")
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            quota["connect_call"]("toy", "GetCurrentPeriodUsage")
        assert error.value.code == 302
        assert reached == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_macos_reads_the_cli_keychain_entry_without_touching_files(tmp_path, monkeypatch):
    fake = tmp_path / "security"
    fake.write_text('#!/bin/sh\n[ "$*" = "find-generic-password -s cursor-access-token -a cursor-user -w" ] && echo " toy-mac-token"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(quota["sys"], "platform", "darwin")
    assert quota["read_token"]() == "toy-mac-token"
    fake.write_text("#!/bin/sh\nexit 44\n")
    assert quota["read_token"]() is None, "no Keychain entry is no login, not an error"
