"""Environments and scheduled tasks (triggers), offline through httpx.MockTransport."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from claude_rc import client as client_mod
from claude_rc.client import CCR_BETA, TRIGGERS_BETA, APIError, RemoteControlClient
from claude_rc.environments import (
    bridge_machines,
    cloud_environment_body,
    default_environment,
    environment_target,
    redact_environment,
)
from claude_rc.triggers import (
    ROUTINE_FIRE_BETA,
    fire_routine,
    rfc3339,
    trigger_body,
    trigger_prompt,
    trigger_url,
)

AUTH_ENV = (
    "CLAUDE_AI_ACCESS_TOKEN",
    "CLAUDE_RC_ACCESS_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_AI_SESSION_KEY",
    "CLAUDE_RC_CSWAP_SLOT",
    "CLAUDE_AI_ORG_UUID",
    "CLAUDE_RC_ORG_UUID",
    "CLAUDE_TRUSTED_DEVICE_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def _client(handler, **kw) -> RemoteControlClient:
    kw.setdefault("org_uuid", "org-1")
    kw.setdefault("token", "tok")
    return RemoteControlClient(transport=httpx.MockTransport(handler), **kw)


def _recorder(responses):
    """Handler answering from ``responses`` (a list of (status, body)) in order."""
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        status, body = responses[len(seen) - 1]
        if isinstance(body, (dict, list)):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body or "")

    return seen, handler


# --- environments ------------------------------------------------------------
ENVS = [
    {"environment_id": "env_bridge", "name": "laptop", "kind": "bridge"},
    {"environment_id": "env_byoc", "name": "byoc", "kind": "byoc"},
    {"environment_id": "env_cloud", "name": "Default", "kind": "anthropic_cloud"},
]


def test_list_environments_follows_after_id_until_has_more_false():
    seen, handler = _recorder([
        (200, {"environments": ENVS[:2], "has_more": True, "last_id": "env_byoc"}),
        (200, {"environments": ENVS[2:], "has_more": False, "last_id": "env_cloud"}),
    ])
    envs = _client(handler).list_environments()
    assert [e["environment_id"] for e in envs] == ["env_bridge", "env_byoc", "env_cloud"]
    assert seen[0].url.path == "/v1/environment_providers"
    assert dict(seen[0].url.params) == {"limit": "100"}
    assert dict(seen[1].url.params) == {"limit": "100", "after_id": "env_byoc"}
    assert seen[0].headers["x-organization-uuid"] == "org-1"
    assert seen[0].headers["authorization"] == "Bearer tok"


def test_list_environments_stops_without_last_id():
    seen, handler = _recorder([(200, {"environments": ENVS, "has_more": True, "last_id": None})])
    assert len(_client(handler).list_environments()) == 3
    assert len(seen) == 1


def test_list_environments_kind_filter_and_bridge_helper():
    _, handler = _recorder([(200, {"environments": ENVS})])
    assert [e["environment_id"] for e in _client(handler).list_environments(kind="bridge")] == ["env_bridge"]
    assert bridge_machines(ENVS) == [ENVS[0]]


def test_default_environment_matches_cli_rule():
    assert default_environment(ENVS)["environment_id"] == "env_cloud"
    assert default_environment(ENVS[:2])["environment_id"] == "env_byoc"
    assert default_environment(ENVS[:1])["environment_id"] == "env_bridge"
    assert default_environment(ENVS, "env_bridge")["environment_id"] == "env_bridge"
    assert default_environment([]) is None


def test_environment_target_pool_vs_env():
    assert environment_target("ccpool_x") == {"self_hosted_runner_pool_id": "ccpool_x"}
    assert environment_target("env_x") == {"environment_id": "env_x"}


def test_create_cloud_environment_default_body_is_the_clis():
    seen, handler = _recorder([(200, {"environment_id": "env_new", "kind": "anthropic_cloud"})])
    out = _client(handler).create_cloud_environment()
    assert out["environment_id"] == "env_new"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/environment_providers/cloud/create"
    assert CCR_BETA in req.headers["anthropic-beta"]
    assert json.loads(req.content) == {
        "name": "Default", "kind": "anthropic_cloud", "description": "Default - trusted network access",
        "config": {"environment_type": "anthropic", "cwd": "/home/user", "init_script": None, "environment": {},
                   "languages": [{"name": "python", "version": "3.11"}, {"name": "node", "version": "20"}],
                   "network_config": {"allowed_hosts": [], "allow_default_hosts": True}}}


def test_create_cloud_environment_overrides():
    body = cloud_environment_body(
        "open", description="", cwd="/work", init_script="make deps", environment={"K": "v"},
        languages=[{"name": "go", "version": "1.23"}], allowed_hosts=["*"], allow_default_hosts=False,
        allow_mcp_servers=True)
    assert body["description"] == ""
    assert body["config"]["cwd"] == "/work" and body["config"]["init_script"] == "make deps"
    assert body["config"]["environment"] == {"K": "v"}
    assert body["config"]["languages"] == [{"name": "go", "version": "1.23"}]
    assert body["config"]["network_config"] == {"allowed_hosts": ["*"], "allow_default_hosts": False,
                                                "allow_mcp_servers": True}


def test_create_cloud_environment_not_retried_on_5xx():
    seen, handler = _recorder([(500, "boom"), (200, {})])
    with pytest.raises(APIError) as e:
        _client(handler).create_cloud_environment()
    assert e.value.status == 500 and len(seen) == 1


def test_get_environment_uses_private_org_route_and_redacts():
    env = {"environment_id": "env_1", "kind": "anthropic_cloud",
           "config": {"environment": {"SECRET": "value"}, "cwd": "/home/user"}}
    seen, handler = _recorder([(200, env)])
    got = _client(handler).get_environment("env_1")
    assert seen[0].url.path == "/v1/environment_providers/private/organizations/org-1/environments/env_1"
    red = redact_environment(got)
    assert red["config"]["environment"] == {"SECRET": "<redacted>"}
    assert got["config"]["environment"] == {"SECRET": "value"}  # original untouched


def test_list_org_environments_limit_1000():
    seen, handler = _recorder([(200, {"environments": ENVS})])
    assert len(_client(handler).list_org_environments()) == 3
    assert seen[0].url.path == "/v1/environment_providers/private/organizations/org-1/environments"
    assert dict(seen[0].url.params) == {"limit": "1000"}


def test_last_used_environment_404_is_none():
    seen, handler = _recorder([(404, "404 page not found\n")])
    assert _client(handler).last_used_environment() is None
    assert seen[0].url.path == "/v1/code/last_used_environment"


def test_last_used_environment_200():
    _, handler = _recorder([(200, {"environment_id": "env_1"})])
    assert _client(handler).last_used_environment() == {"environment_id": "env_1"}


def test_self_hosted_pools():
    pools = [{"pool_id": "ccpool_1", "name": "p", "alive_runner_count": 2}]
    seen, handler = _recorder([(200, {"pools": pools}), (200, {"pool": pools[0]})])
    c = _client(handler)
    assert c.list_self_hosted_pools() == pools
    assert c.get_self_hosted_pool("ccpool_1") == pools[0]
    assert seen[0].url.path == "/v1/code/runners/self-hosted/pools"
    assert seen[1].url.path == "/v1/code/runners/self-hosted/pools/ccpool_1"
    assert "x-organization-uuid" not in seen[1].headers


# --- triggers ------------------------------------------------------------------
def test_trigger_body_cron_and_xor():
    body = trigger_body("daily", environment_id="env_1", prompt="do it", cron_expression="0 9 * * 1-5",
                        model="claude-opus-5-5", repos=["https://github.com/o/r"], allowed_tools=["Bash"])
    assert body["name"] == "daily" and body["cron_expression"] == "0 9 * * 1-5" and body["enabled"] is True
    assert "run_once_at" not in body
    ccr = body["job_config"]["ccr"]
    assert ccr["environment_id"] == "env_1"
    assert ccr["session_context"] == {"model": "claude-opus-5-5", "allowed_tools": ["Bash"],
                                      "sources": [{"git_repository": {"url": "https://github.com/o/r"}}]}
    data = ccr["events"][0]["data"]
    assert data["type"] == "user" and data["session_id"] == "" and data["parent_tool_use_id"] is None
    assert data["message"] == {"content": "do it", "role": "user"} and data["uuid"]
    with pytest.raises(ValueError):
        trigger_body("x", environment_id="e", prompt="p")
    with pytest.raises(ValueError):
        trigger_body("x", environment_id="e", prompt="p", cron_expression="0 * * * *", run_once_at="2026-11-01T00:00:00Z")


def test_rfc3339():
    assert rfc3339(datetime(2026, 11, 1, 9, 0, 0, 123, tzinfo=timezone.utc)) == "2026-11-01T09:00:00Z"
    assert rfc3339(datetime(2026, 11, 1, 9)) == "2026-11-01T09:00:00Z"
    assert rfc3339("2026-11-01T09:00:00Z") == "2026-11-01T09:00:00Z"


def test_create_trigger_sends_beta_and_body():
    seen, handler = _recorder([(200, {"id": "trig_1", "enabled": True})])
    out = _client(handler).create_trigger("once", environment_id="env_1", prompt="hi",
                                          run_once_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert out["id"] == "trig_1"
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/v1/code/triggers"
    assert TRIGGERS_BETA in req.headers["anthropic-beta"].split(",")
    assert req.headers["x-organization-uuid"] == "org-1"
    assert json.loads(req.content)["run_once_at"] == "2026-11-01T00:00:00Z"


def test_list_get_triggers_and_paging():
    seen, handler = _recorder([
        (200, {"data": [{"id": "trig_1"}], "next_cursor": "c2"}),
        (200, {"data": [{"id": "trig_2"}], "next_cursor": None}),
        (200, {"id": "trig_1"}),
    ])
    c = _client(handler)
    assert [t["id"] for t in c.list_triggers()] == ["trig_1", "trig_2"]
    assert dict(seen[1].url.params) == {"cursor": "c2"}
    assert c.get_trigger("trig_1") == {"id": "trig_1"}
    assert seen[2].url.path == "/v1/code/triggers/trig_1"
    assert all(TRIGGERS_BETA in r.headers["anthropic-beta"] for r in seen)


def test_update_enable_disable_trigger_are_partial_posts():
    seen, handler = _recorder([(200, {}), (200, {}), (200, {})])
    c = _client(handler)
    c.update_trigger("trig_1", name="new", run_once_at=datetime(2026, 12, 1, tzinfo=timezone.utc))
    c.enable_trigger("trig_1")
    c.disable_trigger("trig_1")
    assert all(r.method == "POST" and r.url.path == "/v1/code/triggers/trig_1" for r in seen)
    assert json.loads(seen[0].content) == {"name": "new", "run_once_at": "2026-12-01T00:00:00Z"}
    assert json.loads(seen[1].content) == {"enabled": True}
    assert json.loads(seen[2].content) == {"enabled": False}
    with pytest.raises(ValueError):
        c.update_trigger("trig_1", bogus=1)


def test_run_trigger_webhook_and_runs():
    seen, handler = _recorder([(200, {}), (200, {"id": "wh_1"}), (200, {"data": [], "next_cursor": None})])
    c = _client(handler)
    c.run_trigger("trig_1")
    c.create_webhook_trigger("trig_1", source="github", events=["push"])
    runs = c.trigger_runs("trig_1", cursor="c")
    assert seen[0].url.path == "/v1/code/triggers/trig_1/run" and json.loads(seen[0].content) == {}
    assert seen[1].url.path == "/v1/code/webhook-triggers"
    assert json.loads(seen[1].content) == {"source": "github", "events": ["push"], "routine_trigger_id": "trig_1"}
    assert seen[2].url.path == "/v1/code/sessions"
    assert dict(seen[2].url.params) == {"trigger_id": "trig_1", "limit": "10", "cursor": "c"}
    assert runs == {"data": [], "next_cursor": None}


def test_no_delete_trigger():
    assert not hasattr(RemoteControlClient, "delete_trigger")
    assert trigger_url("trig_1") == "https://claude.ai/code/routines/trig_1"


def test_trigger_prompt_reads_both_forms():
    created = trigger_body("x", environment_id="e", prompt="from create", cron_expression="0 * * * *")
    assert trigger_prompt(created) == "from create"
    listed = {"session_request": {"events": [{"payload": {"message": {
        "role": "user", "content": [{"type": "text", "text": "from list"}]}}}]}}
    assert trigger_prompt(listed) == "from list"
    assert trigger_prompt({}) is None


# --- official routine fire API ---------------------------------------------------
def test_fire_routine_uses_its_own_token_and_beta():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"session_id": "s"})

    out = fire_routine("rt_1", "routine-tok", "go", transport=httpx.MockTransport(handler))
    assert out == {"session_id": "s"}
    req = seen[0]
    assert str(req.url) == "https://api.anthropic.com/v1/claude_code/routines/rt_1/fire"
    assert req.headers["authorization"] == "Bearer routine-tok"
    assert req.headers["anthropic-beta"] == ROUTINE_FIRE_BETA
    assert "x-organization-uuid" not in req.headers
    assert json.loads(req.content) == {"text": "go"}


def test_fire_routine_error_and_empty_body():
    def handler(req):
        return httpx.Response(403, json={"error": {"type": "permission_error"}})

    with pytest.raises(APIError) as e:
        fire_routine("rt_1", "t", transport=httpx.MockTransport(handler))
    assert e.value.status == 403 and e.value.error_type == "permission_error"

    seen = []

    def ok(req):
        seen.append(req)
        return httpx.Response(204)

    assert fire_routine("rt_1", "t", transport=httpx.MockTransport(ok)) is None
    assert json.loads(seen[0].content) == {}
