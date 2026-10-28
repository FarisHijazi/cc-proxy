"""claude-rc CLI subcommands, offline: each command's request shapes through a
MockTransport-backed client (the CLI's ``_rc`` factory is swapped out)."""
import json
import time

import httpx
import pytest

from claude_rc import cli
from claude_rc.client import RemoteControlClient

SID = "cse_1"
ORG = "00000000-0000-0000-0000-000000000001"

QUESTION = {
    "type": "control_request",
    "request_id": "req-q",
    "request": {
        "subtype": "can_use_tool",
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu_q",
        "input": {"questions": [
            {"question": "Colour?", "header": "Colour", "multiSelect": False,
             "options": [{"label": "Red"}, {"label": "Blue"}]},
            {"question": "Toppings?", "header": "Top", "multiSelect": True,
             "options": [{"label": "Ham"}, {"label": "Egg"}]},
        ]},
    },
}
PERMISSION = {
    "type": "control_request",
    "request_id": "req-p",
    "request": {"subtype": "can_use_tool", "tool_name": "Bash", "tool_use_id": "toolu_p",
                "input": {"command": "ls"}},
}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("CLAUDE_AI_ACCESS_TOKEN", "CLAUDE_RC_ACCESS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CLAUDE_AI_SESSION_KEY", "CLAUDE_RC_CSWAP_SLOT", "CLAUDE_TRUSTED_DEVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)


class Server:
    """Records requests; serves canned bodies by (method, path) and a worker
    event log for GET /events."""

    def __init__(self, routes=None, log=()):
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.log = [{"sequence_num": str(i + 1), "source": "worker", "payload": p} for i, p in enumerate(log)]

    def posted_events(self) -> list[dict]:
        out = []
        for r in self.requests:
            if r.method == "POST" and r.url.path.endswith("/events"):
                out += [w["payload"] for w in json.loads(r.content)["events"]]
        return out

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        key = (req.method, req.url.path)
        if key in self.routes:
            return httpx.Response(200, json=self.routes[key])
        if req.url.path.endswith("/events"):
            if req.method == "GET":
                return httpx.Response(200, json={"data": list(reversed(self.log))})
            events = json.loads(req.content)["events"]
            return httpx.Response(200, json={"results": [
                {"event_id": w["payload"]["uuid"], "sequence_num": "99", "duplicate": False} for w in events
            ]})
        return httpx.Response(404, json={"error": {"type": "not_found_error", "message": "nope"}})


def run(monkeypatch, server: Server, *argv: str) -> int:
    client = RemoteControlClient(token="tok", org_uuid=ORG, transport=httpx.MockTransport(server.handler))
    monkeypatch.setattr(cli, "_rc", lambda args: client)
    return cli.main(list(argv))


def test_credential_flags_reach_the_client(monkeypatch):
    seen = {}

    class Spy:
        def __init__(self, **kw):
            seen.update(kw)
            raise SystemExit(0)

    monkeypatch.setattr(cli, "RemoteControlClient", Spy)
    with pytest.raises(SystemExit):
        cli.main(["--token", "t1", "list", "--org", "o1", "--cswap-slot", "3"])
    assert seen == {"org_uuid": "o1", "token": "t1", "session_key": None, "cswap_slot": "3"}


def test_create_sends_model_effort_repo_and_prompt(monkeypatch, capsys):
    srv = Server({("POST", "/v1/code/sessions"): {"id": SID, "status": "running", "title": "t"}})
    assert run(monkeypatch, srv, "create", "--env", "env_1", "--model", "claude-haiku-5-5",
               "--effort", "low", "--repo", "o/r", "--permission-mode", "plan", "hello") == 0
    body = json.loads(srv.requests[0].content)
    assert body["environment_id"] == "env_1"
    assert body["config"]["model"] == "claude-haiku-5-5"
    assert body["config"]["effort_level"] == "low"
    kinds = [e["payload"]["type"] for e in body["events"]]
    assert kinds == ["control_request", "user"]
    assert body["events"][0]["payload"]["request"] == {"subtype": "set_permission_mode", "mode": "plan"}
    assert SID in capsys.readouterr().out


def test_answer_question_select_multi_other(monkeypatch):
    srv = Server(log=[QUESTION])
    assert run(monkeypatch, srv, "answer", SID, "question", "--select", "Blue",
               "--multi", "1:Ham", "--multi", "1:Egg", "--other", "1:Cheese") == 0
    [answer] = srv.posted_events()
    text = json.dumps(answer)
    assert "req-q" in text and "Blue" in text and "Ham" in text and "Cheese" in text


def test_answer_question_skip(monkeypatch):
    srv = Server(log=[QUESTION])
    assert run(monkeypatch, srv, "answer", SID, "question", "--skip") == 0
    assert "skipped" in json.dumps(srv.posted_events())


def test_answer_permission_allow_and_deny(monkeypatch):
    srv = Server(log=[PERMISSION])
    assert run(monkeypatch, srv, "answer", SID, "allow") == 0
    [allow] = srv.posted_events()
    assert allow["type"] == "control_response" and "allow" in json.dumps(allow)
    srv2 = Server(log=[PERMISSION])
    assert run(monkeypatch, srv2, "answer", SID, "deny", "--message", "no") == 0
    assert '"deny"' in json.dumps(srv2.posted_events())


def test_answer_without_pending_fails(monkeypatch):
    assert run(monkeypatch, Server(), "answer", SID, "question", "--select", "x") == 1


def test_control_generic_fields(monkeypatch):
    srv = Server()
    assert run(monkeypatch, srv, "control", SID, "set_max_thinking_tokens",
               "--field", "max_thinking_tokens=1024", "--no-wait") == 0
    [req] = srv.posted_events()
    assert req["request"] == {"subtype": "set_max_thinking_tokens", "max_thinking_tokens": 1024}


def test_setters_and_interrupt(monkeypatch):
    srv = Server()
    run(monkeypatch, srv, "model", SID, "claude-opus-5-5")
    run(monkeypatch, srv, "mode", SID, "acceptEdits")
    run(monkeypatch, srv, "interrupt", SID)
    subs = [e["request"]["subtype"] for e in srv.posted_events()]
    assert subs == ["set_model", "set_permission_mode", "interrupt"]


def test_envs_list_json(monkeypatch, capsys):
    srv = Server({("GET", "/v1/environment_providers"): {
        "environments": [{"environment_id": "env_1", "kind": "anthropic_cloud", "name": "Default"}],
        "has_more": False,
    }})
    assert run(monkeypatch, srv, "envs", "list", "--json") == 0
    assert "env_1" in capsys.readouterr().out


def test_triggers_list_and_api_error(monkeypatch, capsys):
    srv = Server({("GET", "/v1/code/triggers"): {"data": [{"id": "trig_1", "name": "n", "enabled": True,
                                                          "cron_expression": "0 9 * * *"}]}})
    assert run(monkeypatch, srv, "triggers", "list") == 0
    assert "trig_1" in capsys.readouterr().out
    assert "ccr-triggers-2026-01-30" in srv.requests[0].headers["anthropic-beta"]
    assert run(monkeypatch, Server(), "triggers", "get", "trig_x") == 1  # 404 -> APIError, exit 1


def test_watch_without_session_is_the_account_feed(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_watch_account", lambda args: called.setdefault("yes", 0))
    assert cli.main(["watch"]) == 0 and "yes" in called


# --- web UI: create session + answer question ---------------------------------
@pytest.fixture
def webui():
    import threading

    from claude_rc import webui as w

    servers = []

    def start(srv: Server):
        client = RemoteControlClient(token="tok", org_uuid=ORG, transport=httpx.MockTransport(srv.handler))
        httpd = w._RCServer(("127.0.0.1", 0), w._Handler, client, False)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_webui_answers_a_question(webui):
    srv = Server(log=[QUESTION])
    base = webui(srv)
    r = httpx.post(f"{base}/api/sessions/{SID}/answer", trust_env=False, json={
        "request_id": "req-q", "selections": {"0": "Red", "1": ["Egg"]}, "other": {"1": "Spam"},
    })
    assert r.status_code == 200, r.text
    text = json.dumps(srv.posted_events())
    assert "Red" in text and "Egg" in text and "Spam" in text
    bad = httpx.post(f"{base}/api/sessions/{SID}/answer", trust_env=False, json={"request_id": "nope", "skip": True})
    assert bad.status_code == 404


def test_webui_creates_a_session_in_the_default_environment(webui):
    srv = Server({
        ("GET", "/v1/environment_providers"): {"environments": [
            {"environment_id": "env_bridge", "kind": "bridge"},
            {"environment_id": "env_cloud", "kind": "anthropic_cloud"},
        ], "has_more": False},
        ("POST", "/v1/code/sessions"): {"id": SID},
    })
    r = httpx.post(f"{webui(srv)}/api/sessions", trust_env=False, json={"prompt": "hi", "model": "claude-haiku-5-5"})
    assert r.status_code == 200, r.text
    body = json.loads(srv.requests[-1].content)
    assert body["environment_id"] == "env_cloud" and body["config"]["model"] == "claude-haiku-5-5"
