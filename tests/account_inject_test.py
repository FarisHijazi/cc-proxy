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


def addon(monkeypatch, calls):
    def slot_token(slot):
        calls.append(slot)
        return f"real-{slot}", time.time() * 1000 + 3_600_000

    monkeypatch.setattr(module, "slot_token", slot_token)
    inject = module.AccountInject()
    inject.accounts = {"personal": "1"}
    monkeypatch.setattr(inject, "identity", lambda alias: "11111111-1111-4111-8111-111111111111")
    return inject


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


def test_native_aggregation_is_opt_in_and_preserves_legacy_clients(monkeypatch):
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


def test_setup_tokens_are_used_as_is_and_never_refreshed(monkeypatch):
    import claude_swap.switcher

    refreshed = []

    class Switcher:
        def current_account_number(self):
            return "1"

        def _get_sequence_data(self):
            return {"accounts": {"3": {"email": "user@example.com"}}}

        def _read_account_credentials_ex(self, slot, email):
            return json.dumps({"claudeAiOauth": {"accessToken": "setup-token", "scopes": ["user:inference"]}}), None

        def consume_backup_grant(self, *args):
            refreshed.append(args)

    monkeypatch.setattr(claude_swap.switcher, "ClaudeAccountSwitcher", Switcher)
    token, expires = module.slot_token("3")
    assert token == "setup-token" and expires > time.time() * 1000
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
