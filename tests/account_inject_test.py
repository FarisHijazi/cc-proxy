"""Server mode: placeholder logins are swapped for the proxy's own tokens."""

import importlib.util
import json
import time
from pathlib import Path

import pytest
from mitmproxy import http, websocket
from mitmproxy.test import tflow

spec = importlib.util.spec_from_file_location(
    "account_inject", Path(__file__).parents[1] / "scripts/account_inject.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture(autouse=True)
def no_real_renewal(monkeypatch):
    # A near-expiry token in any test must never run the real cswap or claude.
    monkeypatch.setattr(module, "renew_in_background", lambda slot, stale: None)


def addon(monkeypatch, calls):
    def slot_token(slot):
        calls.append(slot)
        return f"real-{slot}", time.time() * 1000 + 3_600_000

    monkeypatch.setattr(module, "slot_token", slot_token)
    inject = module.AccountInject()
    inject.accounts = {"personal": "1"}
    monkeypatch.setattr(inject, "identity", lambda alias: "11111111-1111-4111-8111-111111111111")
    return inject


def record_renewals(monkeypatch, renewals):
    """Record renewal threads inline; every other thread (the fetch pool's) still starts for real."""
    import types

    real = module.threading.Thread

    def thread(*args, target=None, **kw):
        if target is module.renew_in_background:
            return types.SimpleNamespace(start=lambda: renewals.append(kw["args"]))
        return real(*args, target=target, **kw)

    monkeypatch.setattr(module.threading, "Thread", thread)


def request(authorization):
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET",
        "https://api.anthropic.com/api/oauth/profile",
        headers={"Authorization": authorization},
    )
    return flow


def test_placeholder_becomes_the_slot_token_and_is_cached(monkeypatch):
    calls = []
    inject = addon(monkeypatch, calls)
    for _ in range(2):
        flow = request("Bearer proxy-account-personal")
        inject.requestheaders(flow)
        assert flow.request.headers["authorization"] == "Bearer real-1"
    assert calls == ["1"]


def test_real_tokens_pass_through(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = request("Bearer sk-ant-oat01-own")
    inject.requestheaders(flow)
    assert flow.request.headers["authorization"] == "Bearer sk-ant-oat01-own"
    assert flow.response is None


def test_unknown_alias_is_answered_locally(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = request("Bearer proxy-account-other")
    inject.requestheaders(flow)
    assert flow.response.status_code == 401
    assert flow.request.headers["authorization"] == "Bearer proxy-account-other"


def test_bridge_connect_frame_gets_the_slot_token(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET", "https://bridge.claudeusercontent.com/chrome/11111111-1111-4111-8111-111111111111",
        headers={"Upgrade": "websocket"},
    )
    flow.websocket = websocket.WebSocketData()
    frame = {"type": "connect", "client_type": "claude-code", "oauth_token": "proxy-account-personal"}
    flow.websocket.messages.append(
        websocket.WebSocketMessage(1, True, json.dumps(frame).encode())
    )
    inject.websocket_message(flow)
    assert json.loads(flow.websocket.messages[-1].text)["oauth_token"] == "real-1"


def test_remote_control_owner_applies_only_to_remote_control_paths(monkeypatch):
    calls = []
    inject = addon(monkeypatch, calls)
    inject.accounts = {"personal": "1", "work": "2"}
    for url, expected in (
        ("https://api.anthropic.com/v1/messages", "Bearer real-2"),
        ("https://api.anthropic.com/api/oauth/profile", "Bearer real-2"),
        ("https://api.anthropic.com/api/oauth/usage", "Bearer real-2"),
        ("https://api.anthropic.com/api/oauth/usage?at_wall=1&skip_spend=1", "Bearer real-2"),
        ("https://api.anthropic.com/v1/code/sessions", "Bearer real-1"),
        ("https://api.anthropic.com/v1/code/sessions/cse_1/worker/events", "Bearer real-1"),
    ):
        flow = tflow.tflow()
        flow.request = http.Request.make(
            "POST", url, headers={"Authorization": "Bearer proxy-account-work+rc=personal"}
        )
        inject.requestheaders(flow)
        assert flow.request.headers["authorization"] == expected, url
    flow = request("Bearer proxy-account-work+rc=nobody")
    inject.requestheaders(flow)
    assert flow.request.headers["authorization"] == "Bearer real-2"
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "POST",
        "https://api.anthropic.com/v1/code/sessions",
        headers={"Authorization": "Bearer proxy-account-work+rc=nobody"},
    )
    inject.requestheaders(flow)
    assert flow.response is not None and flow.response.status_code == 401


def test_private_suffix_is_order_independent_and_never_changes_the_account(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"personal": "1", "work": "2"}
    for placeholder in ("work+rc=personal+private", "work+private+rc=personal"):
        for path, expected in (
            ("/v1/messages", "Bearer real-2"),
            ("/v1/code/sessions", None),
            ("/v1/code/sessions/cse_1/bridge", None),
        ):
            flow = tflow.tflow()
            flow.request = http.Request.make(
                "POST", "https://api.anthropic.com" + path, headers={"Authorization": "Bearer proxy-account-" + placeholder}
            )
            inject.requestheaders(flow)
            assert flow.request.headers.get("authorization") == expected, (placeholder, path)
            assert flow.response is None and flow.metadata["account_private"] is True
    flow = request("Bearer proxy-account-personal")
    inject.requestheaders(flow)
    assert flow.metadata["account_private"] is False


def test_malformed_placeholder_suffix_is_never_forwarded(monkeypatch):
    inject = addon(monkeypatch, [])
    for placeholder in ("personal+shared", "personal+rc=personal+rc=personal"):
        flow = request("Bearer proxy-account-" + placeholder)
        inject.requestheaders(flow)
        assert flow.response.status_code == 401, placeholder


def bridge_frame(inject, token, *, url=None, client_type="claude-code"):
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET", url or "https://bridge.claudeusercontent.com/chrome/11111111-1111-4111-8111-111111111111",
        headers={"Upgrade": "websocket"},
    )
    flow.websocket = websocket.WebSocketData()
    message = websocket.WebSocketMessage(1, True, json.dumps({
        "type": "connect", "client_type": client_type, "oauth_token": token,
    }).encode())
    flow.websocket.messages.append(message)
    flow.live = True
    inject.websocket_message(flow)
    return flow, message


def test_placeholder_aggregation_respects_explicit_selection(monkeypatch):
    from types import SimpleNamespace

    inject = addon(monkeypatch, [])
    inject.accounts["work"] = "2"
    starts = []
    mux = SimpleNamespace(start=lambda *args: starts.append(args))
    monkeypatch.setattr(inject, "bridge", lambda: mux)
    for token in ("own-real-token", "proxy-account-personal", "proxy-account-personal+chrome=personal"):
        flow, message = bridge_frame(inject, token)
        assert not message.dropped and not flow.error
    assert starts == []
    flow, message = bridge_frame(inject, "proxy-account-personal+rc=work+private+chromes")
    assert not message.dropped and json.loads(message.text)["oauth_token"] == "real-1"
    assert starts == [(flow, "personal", None)]
    starts.clear()
    flow, message = bridge_frame(inject, "proxy-account-personal+chrome=work")
    assert starts == [(flow, "personal", "work")]
    assert json.loads(message.text)["oauth_token"] == "real-1"


def test_bad_chrome_authentication_and_path_mismatch_fail_closed(monkeypatch):
    inject = addon(monkeypatch, [])
    for token, url, client in (
        ("proxy-account-personal+chromes", None, "extension"),
        ("proxy-account-personal+chrome=unknown", None, "claude-code"),
        ("proxy-account-personal+chromes+chrome=personal", None, "claude-code"),
        ("proxy-account-personal+chromes", "https://bridge.claudeusercontent.com/chrome/22222222-2222-4222-8222-222222222222", "claude-code"),
    ):
        flow, message = bridge_frame(inject, token, url=url, client_type=client)
        assert message.dropped and flow.error
        assert inject.chrome is None


def test_chrome_tokens_are_not_injected_into_other_endpoints(monkeypatch):
    inject = addon(monkeypatch, [])
    for url in (
        "https://api.anthropic.com/chrome/11111111-1111-4111-8111-111111111111",
        "https://bridge.claudeusercontent.com.evil.test/chrome/11111111-1111-4111-8111-111111111111",
        "https://bridge.claudeusercontent.com/devices/11111111-1111-4111-8111-111111111111/bridge",
        "https://bridge.claudeusercontent.com/chrome/11111111-1111-4111-8111-111111111111?x=1",
        "https://bridge.claudeusercontent.com:444/chrome/11111111-1111-4111-8111-111111111111",
    ):
        flow, message = bridge_frame(inject, "proxy-account-personal+chromes", url=url)
        assert json.loads(message.text)["oauth_token"] == "proxy-account-personal+chromes"
        assert not message.dropped and not flow.error and inject.chrome is None


def test_chrome_suffix_never_changes_http_or_remote_control_routing(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts["work"] = "2"
    for suffix in ("+chromes", "+chrome=work"):
        assert inject.resolve("proxy-account-personal+rc=work" + suffix, "/api/oauth/profile") == "real-1"
        assert inject.resolve("proxy-account-personal+rc=work" + suffix, "/v1/code/sessions") == "real-2"
        assert module.parse("proxy-account-personal+rc=work+private" + suffix) == ("personal", "work", True)


def test_bridge_helper_loads_from_the_script_directory_without_path_changes(monkeypatch):
    inject = addon(monkeypatch, [])
    mux = inject.bridge()
    assert type(mux).__name__ == "ChromeBridge"
    assert inject.bridge() is mux
    inject.done()
    assert not mux.sessions


def test_friendly_chrome_names_use_only_confirmed_account_bound_profiles(monkeypatch, tmp_path):
    inject = addon(monkeypatch, [])
    inject.chrome_profiles_file = tmp_path / "chrome-profiles.json"
    profile = {
        "account": "personal", "deviceId": "browser-a", "confirmed": True,
        "host": "vm-a", "email": "user@example.com",
    }
    inject.chrome_profiles_file.write_text(json.dumps({"profiles": [profile]}))
    assert inject.chrome_label("personal", "browser-a", "Browser 1") == "vm-a - user@example.com"
    assert inject.chrome_label("work", "browser-a", "Browser 1") == "Unidentified browser browser-"
    assert inject.chrome_label("personal", "unknown", "Browser 2") == "Unidentified browser unknown"
    profile["confirmed"] = False
    inject.chrome_profiles_file.write_text(json.dumps({"profiles": [profile]}))
    assert inject.chrome_label("personal", "browser-a", "Browser 1") == "Unidentified browser browser-"


def test_missing_or_invalid_chrome_identity_map_is_marked_unidentified(monkeypatch, tmp_path):
    inject = addon(monkeypatch, [])
    inject.chrome_profiles_file = tmp_path / "missing.json"
    assert inject.chrome_label("personal", "browser-a", "Browser 1") == "Unidentified browser browser-"
    inject.chrome_profiles_file.write_text("{bad-json")
    assert inject.chrome_label("personal", "browser-a", "Browser 1") == "Unidentified browser browser-"


def inference(authorization, path="/v1/messages?beta=true", pool=None):
    flow = tflow.tflow()
    headers = {"Authorization": authorization}
    if pool is not None:
        headers[module.POOL_HEADER] = pool
    flow.request = http.Request.make("POST", "https://api.anthropic.com" + path, headers=headers)
    return flow


def test_inference_carries_every_login_with_the_session_account_first(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2", "c": "3"}
    flow = inference("Bearer proxy-account-b+rc=a")
    inject.requestheaders(flow)
    pool = json.loads(flow.request.headers[module.POOL_HEADER])
    assert [entry["alias"] for entry in pool] == ["b", "a", "c"]
    assert flow.request.headers["authorization"] == "Bearer real-2"
    assert pool[0]["authorization"] == "Bearer real-2"
    assert [entry["primary"] for entry in pool] == [True, False, False]


def test_a_broken_login_is_skipped_and_retried_later(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2"}
    monkeypatch.setattr(module, "slot_token", lambda slot: (_ for _ in ()).throw(ValueError("gone")) if slot == "1" else ("real-2", time.time() * 1000 + 3_600_000))
    flow = inference("Bearer proxy-account-a")
    inject.requestheaders(flow)
    assert [entry["alias"] for entry in json.loads(flow.request.headers[module.POOL_HEADER])] == ["b"]
    assert flow.request.headers["authorization"] == "Bearer real-2"
    assert inject.unusable["a"] > time.time()
    assert json.loads(flow.request.headers[module.POOL_HEADER])[0]["primary"] is False


def test_empty_credential_pool_still_reaches_inference_routing(monkeypatch):
    inject = addon(monkeypatch, [])
    monkeypatch.setattr(module, "slot_token", lambda slot: (_ for _ in ()).throw(ValueError("gone")))
    flow = inference("Bearer proxy-account-personal")
    inject.requestheaders(flow)
    assert flow.response is None
    assert json.loads(flow.request.headers[module.POOL_HEADER]) == []
    assert "authorization" not in flow.request.headers


def test_pool_is_only_for_inference_and_never_taken_from_the_client(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = inference("Bearer proxy-account-personal", "/api/oauth/usage")
    inject.requestheaders(flow)
    assert module.POOL_HEADER not in flow.request.headers
    for authorization in ("Bearer own-real-token", "Bearer proxy-account-personal"):
        flow = inference(authorization, "/api/oauth/profile", pool='[{"alias":"x","authorization":"Bearer stolen"}]')
        inject.requestheaders(flow)
        assert module.POOL_HEADER not in flow.request.headers


def test_unknown_alias_is_refused_for_inference_too(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = inference("Bearer proxy-account-other")
    inject.requestheaders(flow)
    assert flow.response.status_code == 401


def test_new_accounts_join_without_a_restart(monkeypatch, tmp_path):
    inject = addon(monkeypatch, [])
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": {"personal": 1}}))
    inject.read_accounts(path)
    path.write_text(json.dumps({"accounts": {"personal": 1, "extra": 5}}))
    import os
    os.utime(path, (time.time() + 5, time.time() + 5))
    flow = inference("Bearer proxy-account-personal")
    inject.requestheaders(flow)
    assert [entry["alias"] for entry in json.loads(flow.request.headers[module.POOL_HEADER])] == ["personal", "extra"]


def session_pool(inject, session, alias="a"):
    flow = inference(f"Bearer proxy-account-{alias}")
    if session:
        flow.request.headers[module.SESSION_HEADER] = session
    inject.requestheaders(flow)
    return [entry["alias"] for entry in json.loads(flow.request.headers[module.POOL_HEADER])]


def test_prefer_puts_one_main_account_first_for_every_session(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2", "c": "3"}
    inject.strategy, inject.prefer = "prefer", "c"
    assert session_pool(inject, "s1", "a")[0] == "c"
    assert session_pool(inject, None, "b") == ["c", "a", "b"]


def test_round_robin_spreads_sessions_but_never_requests(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2", "c": "3"}
    inject.strategy = "round-robin"
    first = [session_pool(inject, f"s{i}")[0] for i in range(4)]
    assert first == ["a", "b", "c", "a"]
    for _ in range(5):
        assert session_pool(inject, "s1")[0] == "b", "a session keeps its account"


def test_least_sessions_fills_the_emptiest_account(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2"}
    inject.strategy = "least-sessions"
    assert [session_pool(inject, s)[0] for s in ("s1", "s2", "s1", "s3")] == ["a", "b", "a", "a"]


def test_idle_sessions_are_released(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2"}
    inject.strategy = "least-sessions"
    session_pool(inject, "old")
    inject.sticky["old"] = ("a", time.time() - module.STICKY_IDLE_S - 1)
    assert session_pool(inject, "new")[0] == "a", "the idle session no longer counts"


def test_balance_is_read_from_the_accounts_file(monkeypatch, tmp_path):
    import pytest

    inject = addon(monkeypatch, [])
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": {"a": 1, "b": 2}, "balance": {"strategy": "prefer", "prefer": "b"}}))
    inject.read_accounts(path)
    assert (inject.strategy, inject.prefer) == ("prefer", "b")
    for bad in ({"strategy": "per-request"}, {"strategy": "prefer", "prefer": "nobody"}):
        path.write_text(json.dumps({"accounts": {"a": 1}, "balance": bad}))
        with pytest.raises(ValueError):
            inject.read_accounts(path)


def test_email_aliases_and_renamed_old_aliases_resolve(monkeypatch, tmp_path):
    import pytest

    inject = addon(monkeypatch, [])
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({
        "accounts": {"user@example.com": 1, "other.user@example.org": 2},
        "renamed": {"personal": "user@example.com", "work": "other.user@example.org"},
    }))
    inject.read_accounts(path)
    for placeholder, token in [
        ("proxy-account-user@example.com", "Bearer real-1"),
        ("proxy-account-personal", "Bearer real-1"),
        ("proxy-account-work+rc=personal", "Bearer real-2"),
    ]:
        flow = request("Bearer " + placeholder)
        inject.requestheaders(flow)
        assert flow.request.headers["authorization"] == token
    flow = inference("Bearer proxy-account-work")
    inject.requestheaders(flow)
    pool = json.loads(flow.request.headers[module.POOL_HEADER])
    assert [entry["alias"] for entry in pool] == ["other.user@example.org", "user@example.com"]
    path.write_text(json.dumps({"accounts": {"a": 1}, "renamed": {"old": "missing"}}))
    with pytest.raises(ValueError):
        inject.read_accounts(path)


def test_cascade_forwards_aliases_held_by_the_next_proxy(monkeypatch):
    calls = []
    inject = addon(monkeypatch, calls)
    inject.upstream = module.server_spec.parse("http://next.example:18790", "http")
    for flow in (request("Bearer proxy-account-other"), inference("Bearer proxy-account-other")):
        inject.requestheaders(flow)
        assert flow.response is None
        assert flow.request.headers["authorization"] == "Bearer proxy-account-other"
        assert module.POOL_HEADER not in flow.request.headers
    flow = request("Bearer proxy-account-personal")
    inject.requestheaders(flow)
    assert flow.request.headers["authorization"] == "Bearer real-1"
    flow = request("Bearer proxy-account-other+bogus")
    inject.requestheaders(flow)
    assert flow.response.status_code == 401
    assert calls == ["1"]


def test_cascade_sends_the_chrome_bridge_through_the_next_proxy(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.upstream = module.server_spec.parse("http://next.example:18790", "http")
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET", "https://bridge.claudeusercontent.com/chrome/11111111-1111-4111-8111-111111111111",
        headers={"Upgrade": "websocket"},
    )
    inject.requestheaders(flow)
    assert flow.server_conn.via == inject.upstream
    flow.websocket = websocket.WebSocketData()
    frame = {"type": "connect", "client_type": "claude-code", "oauth_token": "proxy-account-other"}
    flow.websocket.messages.append(websocket.WebSocketMessage(1, True, json.dumps(frame).encode()))
    inject.websocket_message(flow)
    assert json.loads(flow.websocket.messages[-1].text)["oauth_token"] == "proxy-account-other"
    assert not flow.websocket.messages[-1].dropped


def test_pinned_sessions_start_on_their_own_account_whatever_the_balance(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2", "c": "3"}
    inject.strategy, inject.prefer = "prefer", "b"
    assert session_pool(inject, "s1", "c") == ["b", "a", "c"]
    flow = inference("Bearer proxy-account-c+pin+rc=a")
    flow.request.headers[module.SESSION_HEADER] = "s2"
    inject.requestheaders(flow)
    pool = json.loads(flow.request.headers[module.POOL_HEADER])
    assert [e["alias"] for e in pool] == ["c", "a", "b"]
    assert flow.request.headers["authorization"] == "Bearer real-3"


def test_disabled_accounts_are_left_out_of_fallback(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2", "c": "3"}
    inject.disabled = {"b"}
    assert session_pool(inject, "s1", "a") == ["a", "c"]


def test_spreading_and_prefer_skip_disabled_accounts(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2", "c": "3"}
    inject.disabled = {"b"}
    inject.strategy = "round-robin"
    assert [session_pool(inject, s, "a")[0] for s in ("s1", "s2", "s3")] == ["a", "c", "a"]
    inject.strategy, inject.prefer = "prefer", "b"
    assert session_pool(inject, "s4", "c")[0] == "c"


@pytest.mark.parametrize("active", [False, True], ids=["inactive", "active"])
def test_setup_tokens_are_used_as_is_and_never_refreshed(monkeypatch, active):
    import claude_swap.switcher

    refreshed = []

    class Switcher:
        def current_account_number(self):
            return "3" if active else "1"

        def _read_credentials(self):
            return json.dumps({"claudeAiOauth": {"accessToken": "setup-token", "scopes": ["user:inference"]}})

        def _get_sequence_data(self):
            return {"accounts": {"3": {"email": "user@example.com"}}}

        def _read_account_credentials_ex(self, slot, email):
            return json.dumps({"claudeAiOauth": {"accessToken": "setup-token", "scopes": ["user:inference"]}}), None

        def consume_backup_grant(self, *args):
            refreshed.append(args)

    monkeypatch.setattr(claude_swap.switcher, "ClaudeAccountSwitcher", Switcher)
    token, expires = module.slot_token("3")
    assert token == "setup-token" and expires > time.time() * 1000
    assert expires <= time.time() * 1000 + 61_000, "a replaced setup-token must take effect within a minute"
    assert refreshed == []


def test_expired_active_oauth_never_consumes_a_refresh_grant(monkeypatch):
    import claude_swap.switcher

    refreshed = []

    class Switcher:
        def current_account_number(self):
            return "3"

        def _read_credentials(self):
            return json.dumps({"claudeAiOauth": {
                "accessToken": "expired-token", "expiresAt": 1, "refreshToken": "live-grant",
            }})

        def consume_backup_grant(self, *args):
            refreshed.append(args)

    monkeypatch.setattr(claude_swap.switcher, "ClaudeAccountSwitcher", Switcher)
    with pytest.raises(ValueError):
        module.slot_token("3")
    assert refreshed == []


def test_fallback_policy_rides_with_the_pool_and_is_never_taken_from_the_client(monkeypatch):
    inject = addon(monkeypatch, [])
    flow = inference("Bearer proxy-account-personal")
    flow.request.headers[module.POLICY_HEADER] = '{"chains":{"claude":["stolen"]}}'
    inject.requestheaders(flow)
    assert module.POLICY_HEADER not in flow.request.headers
    inject.fallback = {"chains": {"claude": ["accounts", "codex"]}, "on": {"server_error": True}}
    flow = inference("Bearer proxy-account-personal")
    inject.requestheaders(flow)
    assert json.loads(flow.request.headers[module.POLICY_HEADER]) == inject.fallback
    flow = inference("Bearer proxy-account-personal", "/api/oauth/usage")
    inject.requestheaders(flow)
    assert module.POLICY_HEADER not in flow.request.headers


def answered(inject, alias, five_hour, reset=None):
    flow = inference("Bearer proxy-account-a")
    inject.requestheaders(flow)
    flow.response = http.Response.make(200, b"", {
        module.SERVED_BY_HEADER: f"{alias}/claude-opus-5-5",
        "anthropic-ratelimit-unified-5h-utilization": str(five_hour),
        "anthropic-ratelimit-unified-5h-reset": str(reset or int(time.time()) + 3600),
        "anthropic-ratelimit-unified-7d-utilization": "0.1",
    })
    inject.responseheaders(flow)


def test_accounts_at_the_usage_threshold_are_tried_last(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2", "c": "3"}
    answered(inject, "a", 0.95)
    assert session_pool(inject, None) == ["a", "b", "c"], "no threshold: today's order"
    inject.fallback = {"threshold": 90}
    flow = inference("Bearer proxy-account-a")
    inject.requestheaders(flow)
    pool = json.loads(flow.request.headers[module.POOL_HEADER])
    assert [e["alias"] for e in pool] == ["b", "c", "a"]
    assert [e["primary"] for e in pool] == [False, False, True], "the thread owner never changes"
    assert flow.request.headers["authorization"] == "Bearer real-2"
    answered(inject, "a", 0.5)
    assert session_pool(inject, None) == ["a", "b", "c"]
    answered(inject, "a", 0.99, reset=int(time.time()) - 1)
    assert session_pool(inject, None) == ["a", "b", "c"], "a window that already reset is ignored"


def test_fallback_section_is_read_and_a_bad_threshold_is_refused(monkeypatch, tmp_path):
    inject = addon(monkeypatch, [])
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": {"a": 1}, "fallback": {"threshold": 80}}))
    inject.read_accounts(path)
    assert inject.fallback == {"threshold": 80}
    path.write_text(json.dumps({"accounts": {"a": 1}, "fallback": {"threshold": 150}}))
    with pytest.raises(ValueError):
        inject.read_accounts(path)


def test_counts_carry_the_same_pool_as_messages(monkeypatch):
    """A model one login lacks 404s its count too, so counts fall back like messages."""
    inject = addon(monkeypatch, [])
    inject.accounts = {"a": "1", "b": "2"}
    pools = []
    for path in ("/v1/messages?beta=true", "/v1/messages/count_tokens?beta=true"):
        flow = inference("Bearer proxy-account-b", path, pool='[{"alias":"x","authorization":"Bearer stolen"}]')
        inject.requestheaders(flow)
        assert flow.request.headers["authorization"] == "Bearer real-2"
        assert flow.metadata["account_pool"] is True
        pools.append(json.loads(flow.request.headers[module.POOL_HEADER]))
    assert pools[0] == pools[1]
    assert [entry["alias"] for entry in pools[1]] == ["b", "a"]


def test_model_listing_without_a_login_borrows_the_first_enabled_account(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.accounts = {"off": "2", "personal": "1"}
    inject.disabled = {"off"}
    flow = tflow.tflow()
    flow.request = http.Request.make("GET", "https://api.anthropic.com/v1/models?limit=1000")
    inject.requestheaders(flow)
    assert flow.request.headers["authorization"] == "Bearer real-1"
    other = tflow.tflow()
    other.request = http.Request.make("GET", "https://api.anthropic.com/api/oauth/profile")
    inject.requestheaders(other)
    assert "authorization" not in other.request.headers


def test_model_listing_borrows_nothing_when_the_next_proxy_holds_the_logins(monkeypatch):
    inject = addon(monkeypatch, [])
    inject.upstream = ("http", ("127.0.0.1", 1))
    flow = tflow.tflow()
    flow.request = http.Request.make("GET", "https://api.anthropic.com/v1/models")
    inject.requestheaders(flow)
    assert "authorization" not in flow.request.headers


def keyed(inject, tmp_path, keys, main="b"):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"main": main, "accounts": {"a": 1, "b": 2},
                                "api_keys": [{"name": n, "sha256": module.key_hash(k)} for n, k in keys.items()]}))
    inject.read_accounts(path)


@pytest.mark.parametrize("header", ["x-api-key", "authorization"])
def test_an_api_key_uses_the_account_pool_from_the_main_account(monkeypatch, tmp_path, header):
    inject = addon(monkeypatch, [])
    keyed(inject, tmp_path, {"laptop": "ccp-secret"})
    flow = tflow.tflow()
    value = "ccp-secret" if header == "x-api-key" else "Bearer ccp-secret"
    flow.request = http.Request.make("POST", "https://api.anthropic.com/v1/messages", headers={header: value})
    inject.requestheaders(flow)
    assert flow.response is None
    assert "x-api-key" not in flow.request.headers
    assert flow.request.headers["authorization"] == "Bearer real-2"
    assert [e["alias"] for e in json.loads(flow.request.headers[module.POOL_HEADER])] == ["b", "a"]
    assert flow.metadata["api_key"] == "laptop"


def test_an_unknown_or_revoked_api_key_is_refused_locally(monkeypatch, tmp_path):
    inject = addon(monkeypatch, [])
    keyed(inject, tmp_path, {"laptop": "ccp-secret"})
    flow = tflow.tflow()
    flow.request = http.Request.make("POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": "ccp-wrong"})
    inject.requestheaders(flow)
    assert flow.response.status_code == 401
    assert b"ccp-wrong" not in flow.response.content


def test_real_anthropic_api_keys_pass_through(monkeypatch, tmp_path):
    inject = addon(monkeypatch, [])
    keyed(inject, tmp_path, {"laptop": "ccp-secret"})
    flow = tflow.tflow()
    flow.request = http.Request.make("POST", "https://api.anthropic.com/v1/messages", headers={"x-api-key": "sk-ant-api03-own"})
    inject.requestheaders(flow)
    assert flow.response is None
    assert flow.request.headers["x-api-key"] == "sk-ant-api03-own"


def test_a_rejected_injected_token_is_dropped_and_renewed_once_a_minute(monkeypatch):
    calls, renewals = [], []
    inject = addon(monkeypatch, calls)
    record_renewals(monkeypatch, renewals)
    for _ in range(2):
        flow = request("Bearer proxy-account-personal")
        inject.requestheaders(flow)
        flow.response = http.Response.make(401, b"")
        inject.responseheaders(flow)
    assert calls == ["1", "1"], "the rejected token is re-read, never reused"
    assert renewals == [("1", "real-1")], "a burst of rejections renews the slot once"


def test_a_401_for_a_token_already_replaced_changes_nothing(monkeypatch):
    calls = []
    inject = addon(monkeypatch, calls)
    flow = request("Bearer proxy-account-personal")
    inject.requestheaders(flow)
    inject.cache["personal"] = ("newer", time.time() * 1000 + 3_600_000)
    flow.response = http.Response.make(401, b"")
    inject.responseheaders(flow)
    assert inject.cache["personal"][0] == "newer"


def switcher_with(live_token, live_expires, done):
    def creds(token, expires=None):
        oauth = {"accessToken": token, "refreshToken": "grant"}
        if expires is not None:
            oauth["expiresAt"] = expires
        return json.dumps({"claudeAiOauth": oauth})

    class Switcher:
        def current_account_number(self):
            return "1"

        def _get_sequence_data(self):
            return {"accounts": {"1": {"email": "user@example.com"}, "2": {"email": "other@example.com"}}}

        def _read_credentials(self):
            return creds(live_token, live_expires)

        def _read_account_credentials_ex(self, slot, email):
            return creds("backup-token"), None

        def consume_backup_grant(self, slot, email, raw):
            done.append(("backup", slot))

    return Switcher


def test_renew_acts_only_on_the_rejected_or_expiring_credential(monkeypatch):
    import claude_swap.switcher

    done = []
    monkeypatch.setattr(module, "renew_live_login", lambda: done.append(("live", "1")))
    hour = time.time() * 1000 + 3_600_000
    monkeypatch.setattr(claude_swap.switcher, "ClaudeAccountSwitcher", switcher_with("live-token", hour, done))
    module.renew("1", "older-token")
    module.renew("2", "older-token")
    module.renew("1", None)
    module.renew("2", None)
    assert done == [], "a credential that already moved on, or is far from expiry, is left alone"
    module.renew("1", "live-token")
    module.renew("2", "backup-token")
    assert done == [("live", "1"), ("backup", "2")]
    done.clear()
    for expires in (time.time() * 1000 + 60_000, time.time() * 1000 - 60_000):
        monkeypatch.setattr(claude_swap.switcher, "ClaudeAccountSwitcher", switcher_with("live-token", expires, done))
        module.renew("1", None)
    assert done == [("live", "1"), ("live", "1")], "an expiring or expired live login is renewed"
    done.clear()
    monkeypatch.setattr(claude_swap.switcher, "ClaudeAccountSwitcher", switcher_with("setup-token", None, done))
    module.renew("1", None)
    assert done == [], "a setup-token has no expiry to renew"


def test_an_expiring_or_expired_login_is_renewed_once_a_minute(monkeypatch):
    renewals = []
    record_renewals(monkeypatch, renewals)
    inject = addon(monkeypatch, [])
    monkeypatch.setattr(module, "slot_token", lambda slot: ("soon", time.time() * 1000 + 60_000))
    for _ in range(3):
        assert inject.token("personal") == "soon", "the token is still served until it expires"
    assert renewals == [("1", None)]
    inject.renewed.clear()
    inject.fetching.clear()
    inject.cache["personal"] = ("gone", time.time() * 1000 - 1)
    monkeypatch.setattr(module, "slot_token", lambda slot: (_ for _ in ()).throw(ValueError("expired")))
    with pytest.raises(ValueError):
        inject.token("personal")
    assert renewals == [("1", None), ("1", None)], "an already expired login is renewed too"


def test_claude_code_renews_the_live_login_without_the_proxy_env(monkeypatch):
    runs = []
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kw: runs.append((argv, kw)))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    module.renew_live_login()
    (argv, kw), = runs
    assert argv[:2] == ["claude", "-p"]
    assert not module.PROXY_ENV & set(kw["env"]), "Claude Code must reach Anthropic directly"
    assert kw["check"] and kw["timeout"]


def test_an_expiring_token_keeps_serving_while_it_refreshes_in_the_background(monkeypatch):
    import threading

    release = threading.Event()

    def slow(slot):
        release.wait(10)
        return "fresh", time.time() * 1000 + 3_600_000

    inject = addon(monkeypatch, [])
    monkeypatch.setattr(inject, "renew", lambda alias, stale: None)
    inject.cache["personal"] = ("expiring", time.time() * 1000 + 60_000)
    monkeypatch.setattr(module, "slot_token", slow)
    started = time.monotonic()
    assert inject.token("personal") == "expiring", "a still-valid token is served, never waited on"
    assert inject.token("personal") == "expiring"
    assert time.monotonic() - started < 0.1
    release.set()
    inject.fetching["personal"].result(timeout=5)
    assert inject.token("personal") == "fresh"


def test_a_slow_fetch_leaves_an_account_out_of_one_pool_without_marking_it_unusable(monkeypatch):
    import threading

    release = threading.Event()

    def slot_token(slot):
        if slot == "2":
            release.wait(10)
        return f"real-{slot}", time.time() * 1000 + 3_600_000

    inject = addon(monkeypatch, [])
    inject.accounts = {"personal": "1", "work": "2"}
    monkeypatch.setattr(module, "slot_token", slot_token)
    monkeypatch.setattr(module, "FETCH_WAIT_S", 0.05)
    assert [entry["alias"] for entry in inject.pool("personal")] == ["personal"]
    assert "work" not in inject.unusable, "still fetching is not broken"
    release.set()
    inject.fetching["work"].result(timeout=5)
    assert [entry["alias"] for entry in inject.pool("personal")] == ["personal", "work"]


def test_a_pool_with_every_login_still_fetching_asks_the_client_to_retry(monkeypatch):
    import threading

    release = threading.Event()
    inject = addon(monkeypatch, [])
    monkeypatch.setattr(module, "slot_token", lambda slot: release.wait(10) and ("late", 0))
    monkeypatch.setattr(module, "FETCH_WAIT_S", 0.05)
    with pytest.raises(module.StillFetching):
        inject.pool("personal")
    release.set()
    assert not inject.unusable


def test_a_fetch_started_before_a_rejection_never_restores_the_rejected_token(monkeypatch):
    import threading

    release = threading.Event()

    def slow(slot):
        release.wait(10)
        return "rejected", time.time() * 1000 + 3_600_000

    inject = addon(monkeypatch, [])
    monkeypatch.setattr(inject, "renew", lambda alias, stale: None)
    inject.cache["personal"] = ("rejected", time.time() * 1000 + 60_000)
    monkeypatch.setattr(module, "slot_token", slow)
    assert inject.token("personal") == "rejected"
    inject.rejected("personal", "rejected")
    release.set()
    inject.fetching["personal"].result(timeout=5)
    time.sleep(0.05)
    assert "personal" not in inject.cache, "the late fetch must not cache a token Anthropic refused"


def test_shared_login_mode_reloads_the_accounts_file_off_the_event_loop(monkeypatch, tmp_path):
    submitted = []
    inject = addon(monkeypatch, [])
    inject.grants = object()
    path = tmp_path / "accounts.json"
    path.write_text("{}")
    inject.accounts_file, inject.accounts_mtime = path, 0
    monkeypatch.setattr(inject, "read_accounts", lambda p: pytest.fail("read inline on the event loop"))
    monkeypatch.setattr(module.RELOADER, "submit", lambda fn, *args: submitted.append((fn, args)))
    inject.reload_accounts()
    inject.reload_accounts()
    assert submitted == [(inject.reload_in_background, (path,))], "one background reload per change"


def test_a_failed_background_reload_is_retried_on_the_next_request(monkeypatch, tmp_path):
    submitted = []
    inject = addon(monkeypatch, [])
    inject.grants = object()
    path = tmp_path / "accounts.json"
    path.write_text("not json")
    inject.accounts_file, inject.accounts_mtime = path, 0
    monkeypatch.setattr(module.RELOADER, "submit", lambda fn, *args: submitted.append(args) or fn(*args))
    inject.reload_accounts()
    inject.reload_accounts()
    assert submitted == [(path,), (path,)], "a torn read does not wait for the next edit"


FAKE_KEY = "sk-ant-api03-fake-test-key"


def with_key(monkeypatch, tmp_path, key_account, **config):
    inject = addon(monkeypatch, [])
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": {"a": 1, "b": 2}, "api_key_accounts": {"console": key_account}, **config}))
    inject.read_accounts(path)
    return inject


def pool_of(inject, placeholder="Bearer proxy-account-a", session="s1"):
    flow = inference(placeholder)
    flow.request.headers[module.SESSION_HEADER] = session
    inject.requestheaders(flow)
    return flow, json.loads(flow.request.headers[module.POOL_HEADER])


def test_a_preferred_api_key_leads_every_pool_and_the_balance_orders_the_rest(monkeypatch, tmp_path):
    inject = with_key(monkeypatch, tmp_path, {"key": FAKE_KEY, "preferred": True},
                      balance={"strategy": "prefer", "prefer": "b"})
    for session in ("s1", "s1", "s2"):
        flow, pool = pool_of(inject, session=session)
        assert [e["alias"] for e in pool] == ["console", "b", "a"]
        assert pool[0] == {"alias": "console", "kind": "api_key", "api_key": FAKE_KEY, "primary": True}
        assert [e["primary"] for e in pool] == [True, False, False]
        # Only the router puts the key on an attempt; the request itself carries a subscription login.
        assert flow.request.headers["authorization"] == "Bearer real-2"
        assert "x-api-key" not in flow.request.headers


def test_a_key_that_is_not_preferred_or_a_pinned_session_tries_subscriptions_first(monkeypatch, tmp_path):
    inject = with_key(monkeypatch, tmp_path, {"key": FAKE_KEY})
    assert [e["alias"] for e in pool_of(inject)[1]] == ["a", "b", "console"]
    inject = with_key(monkeypatch, tmp_path, {"key": FAKE_KEY, "preferred": True})
    _, pool = pool_of(inject, "Bearer proxy-account-b+pin")
    assert [(e["alias"], e["primary"]) for e in pool] == [("b", True), ("a", False), ("console", False)]


def test_a_disabled_key_is_left_out(monkeypatch, tmp_path):
    inject = with_key(monkeypatch, tmp_path, {"key": FAKE_KEY, "preferred": True}, disabled=["console"])
    assert [e["alias"] for e in pool_of(inject)[1]] == ["a", "b"]


def test_a_key_alone_still_reaches_the_router_without_a_login_on_the_request(monkeypatch, tmp_path):
    inject = with_key(monkeypatch, tmp_path, {"key": FAKE_KEY, "preferred": True})
    monkeypatch.setattr(module, "slot_token", lambda slot: (_ for _ in ()).throw(ValueError("gone")))
    flow, pool = pool_of(inject)
    assert [e["alias"] for e in pool] == ["console"]
    assert "authorization" not in flow.request.headers and "x-api-key" not in flow.request.headers


@pytest.mark.parametrize("key_accounts", [
    {"a": {"key": FAKE_KEY}},
    {"console": {"key": ""}},
    {"console": {"key": FAKE_KEY + "\n"}},
    {"console": {"key": 5}},
    {"console": FAKE_KEY},
    {"console": {"key": FAKE_KEY, "preferred": "yes"}},
    {"bad alias!": {"key": FAKE_KEY}},
])
def test_a_malformed_key_account_is_refused(monkeypatch, tmp_path, key_accounts):
    inject = addon(monkeypatch, [])
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps({"accounts": {"a": 1}, "api_key_accounts": key_accounts}))
    with pytest.raises(ValueError) as error:
        inject.read_accounts(path)
    assert FAKE_KEY not in str(error.value)


def budgeted(monkeypatch, tmp_path, spend):
    inject = with_key(monkeypatch, tmp_path, {"key": FAKE_KEY, "preferred": True, "monthly_budget_usd": 180},
                      traffic_db=str(tmp_path / "traffic.sqlite3"))
    if spend is not None:
        inject.spend["console"] = spend
    return inject


def test_a_budgeted_key_tells_the_router_what_is_left_of_its_monthly_budget(monkeypatch, tmp_path):
    inject = budgeted(monkeypatch, tmp_path, (179.25, time.time()))
    _, pool = pool_of(inject)
    assert pool[0]["alias"] == "console" and pool[0]["budget_usd"] == pytest.approx(0.75)


@pytest.mark.parametrize("spend", [None, (10.0, time.time() - module.SPEND_STALE_S - 1), (180.0, time.time()), (190.0, time.time())],
                         ids=["never-read", "stale", "at-cap", "over-cap"])
def test_a_key_whose_spend_is_unknown_or_at_its_budget_is_left_out(monkeypatch, tmp_path, spend):
    inject = budgeted(monkeypatch, tmp_path, spend)
    _, pool = pool_of(inject)
    assert [e["alias"] for e in pool] == ["a", "b"]
    assert pool[0]["primary"] is True, "the session's subscription owns the thread instead"


@pytest.mark.parametrize("member", [{"monthly_budget_usd": 0}, {"monthly_budget_usd": -5}, {"monthly_budget_usd": "180"},
                                    {"monthly_budget_usd": True}])
def test_a_malformed_budget_is_refused(monkeypatch, tmp_path, member):
    with pytest.raises(ValueError):
        with_key(monkeypatch, tmp_path, {"key": FAKE_KEY, **member}, traffic_db=str(tmp_path / "t.sqlite3"))


def test_a_budget_without_a_traffic_database_leaves_the_key_out_but_the_proxy_serving(monkeypatch, tmp_path):
    # deploy-main.py's canary drops traffic_db; refusing the file there (or on a restart) took mitmdump down.
    inject = with_key(monkeypatch, tmp_path, {"key": FAKE_KEY, "preferred": True, "monthly_budget_usd": 180})
    assert [e["alias"] for e in pool_of(inject)[1]] == ["a", "b"]


def bridge_reply(inject, flow, kind, *, from_client=False, injected=False):
    message = websocket.WebSocketMessage(
        1, from_client, json.dumps({"type": kind}).encode(), injected=injected
    )
    flow.websocket.messages.append(message)
    inject.websocket_message(flow)
    return message


@pytest.mark.parametrize("accepted", ["paired", "waiting"])
def test_own_login_pools_only_after_upstream_accepts_it(monkeypatch, accepted):
    from types import SimpleNamespace

    calls, starts, forwarded = [], [], []
    inject = addon(monkeypatch, calls)
    mux = SimpleNamespace(start=lambda *args: starts.append(args), message=lambda flow: forwarded.append(flow))
    monkeypatch.setattr(inject, "bridge", lambda: mux)
    flow, original = bridge_frame(inject, "own-real-token")
    assert not starts and not calls
    assert json.loads(original.text)["oauth_token"] == "own-real-token"
    assert "own-real-token" not in json.dumps(flow.metadata)
    assert flow.metadata["chrome_pending_alias"] == "personal"
    bridge_reply(inject, flow, "ping")
    bridge_reply(inject, flow, accepted, from_client=True)
    bridge_reply(inject, flow, accepted, injected=True)
    assert not starts
    message = bridge_reply(inject, flow, accepted)
    assert starts == [(flow, "personal")]
    assert forwarded == [flow]
    assert flow.metadata["chrome_authenticated"]
    assert "chrome_pending_alias" not in flow.metadata
    assert not message.dropped and not calls
    bridge_reply(inject, flow, accepted)
    assert len(starts) == 1


def test_rejected_own_login_does_not_reach_other_accounts(monkeypatch):
    inject = addon(monkeypatch, [])
    monkeypatch.setattr(inject, "bridge", lambda: pytest.fail("unauthenticated pooling"))
    flow, original = bridge_frame(inject, "rejected-token")
    bridge_reply(inject, flow, "error")
    assert "chrome_pending_alias" not in flow.metadata
    bridge_reply(inject, flow, "paired")
    assert not flow.metadata.get("chrome_authenticated")
    assert json.loads(original.text)["oauth_token"] == "rejected-token"
    assert inject.chrome is None


@pytest.mark.parametrize("client,token,path", [
    ("extension", "own-real-token", "/chrome/11111111-1111-4111-8111-111111111111"),
    ("claude-code", "", "/chrome/11111111-1111-4111-8111-111111111111"),
    ("claude-code", None, "/chrome/11111111-1111-4111-8111-111111111111"),
    ("claude-code", "own-real-token", "/chrome/22222222-2222-4222-8222-222222222222"),
    ("claude-code", "own-real-token", "/chrome/11111111-1111-4111-8111-111111111111?query=1"),
])
def test_unrelated_own_login_connections_never_pool(monkeypatch, client, token, path):
    inject = addon(monkeypatch, [])
    monkeypatch.setattr(inject, "bridge", lambda: pytest.fail("unexpected pooling"))
    flow, original = bridge_frame(
        inject, token, url="https://bridge.claudeusercontent.com" + path, client_type=client
    )
    bridge_reply(inject, flow, "paired")
    assert "chrome_pending_alias" not in flow.metadata
    assert not flow.metadata.get("chrome_authenticated")
    assert not original.dropped and not flow.error
    assert json.loads(original.text)["oauth_token"] == token


def test_own_login_without_connect_frame_cannot_pool(monkeypatch):
    inject = addon(monkeypatch, [])
    monkeypatch.setattr(inject, "bridge", lambda: pytest.fail("unexpected pooling"))
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET", "https://bridge.claudeusercontent.com/chrome/11111111-1111-4111-8111-111111111111",
        headers={"Upgrade": "websocket"},
    )
    flow.websocket = websocket.WebSocketData()
    bridge_reply(inject, flow, "paired")
    assert not flow.metadata.get("chrome_authenticated")


def test_missing_account_snapshot_does_not_hide_valid_own_login(monkeypatch):
    from types import SimpleNamespace

    inject = addon(monkeypatch, [])
    inject.accounts = {"missing": "0", "personal": "1"}
    def identity(alias):
        if alias == "missing":
            raise FileNotFoundError("no snapshot")
        return "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(inject, "identity", identity)
    starts = []
    mux = SimpleNamespace(start=lambda *args: starts.append(args), message=lambda flow: None)
    monkeypatch.setattr(inject, "bridge", lambda: mux)
    flow, _ = bridge_frame(inject, "own-real-token")
    bridge_reply(inject, flow, "paired")
    assert starts == [(flow, "personal")]


@pytest.mark.parametrize("frame", [
    {"type": "connect", "client_type": "extension", "oauth_token": "other-token"},
    {"type": "connect", "client_type": "claude-code", "oauth_token": ""},
    {"type": "connect", "client_type": "claude-code", "oauth_token": "proxy-account-personal+chrome=personal"},
])
def test_repeated_connect_clears_pending_own_login(monkeypatch, frame):
    inject = addon(monkeypatch, [])
    monkeypatch.setattr(inject, "bridge", lambda: pytest.fail("unexpected pooling"))
    flow, _ = bridge_frame(inject, "own-real-token")
    flow.websocket.messages.append(websocket.WebSocketMessage(1, True, json.dumps(frame).encode()))
    inject.websocket_message(flow)
    assert "chrome_pending_alias" not in flow.metadata
    bridge_reply(inject, flow, "paired")
    assert inject.chrome is None
