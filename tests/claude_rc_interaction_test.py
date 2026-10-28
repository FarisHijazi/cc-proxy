"""InteractionMixin: controls, prompts, AskUserQuestion, slash commands, the
session stream's meta frames and the account watch — offline via MockTransport."""
import json
import time

import httpx
import pytest

from claude_rc import interaction as inter
from claude_rc.client import ControlRejected, RemoteControlClient
from claude_rc.events import (
    Event,
    StreamFrame,
    cli_question_answer,
    cli_user_dialog_response,
    image_block,
    join_answer_labels,
)
from claude_rc.interaction import (
    CLOUD_FLAG_SETTINGS,
    PendingRequest,
    WatchEvent,
    build_answers,
    find_pending,
    is_client_only,
    pending_from_initialize,
)

SID = "cse_1"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("CLAUDE_AI_ACCESS_TOKEN", "CLAUDE_RC_ACCESS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                 "CLAUDE_AI_SESSION_KEY", "CLAUDE_RC_CSWAP_SLOT", "CLAUDE_TRUSTED_DEVICE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(time, "sleep", lambda s: None)


class FakeSession:
    """Records POSTed events and answers every control_request from the worker."""

    def __init__(self, responses=None, errors=None):
        self.posted: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.responses = responses or {}
        self.errors = errors or {}
        self.log: list[dict] = []  # envelopes served by GET /events

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.method == "POST" and req.url.path.endswith("/events"):
            results = []
            for wrapped in json.loads(req.content)["events"]:
                p = wrapped["payload"]
                self.posted.append(p)
                seq = len(self.log) + 1
                self.log.append({"sequence_num": str(seq), "source": "client", "payload": p})
                results.append({"event_id": p["uuid"], "sequence_num": str(seq), "duplicate": False})
                if p["type"] == "control_request":
                    sub = p["request"]["subtype"]
                    if sub in self.errors:
                        inner = {"subtype": "error", "request_id": p["request_id"], "error": self.errors[sub]}
                    else:
                        inner = {"subtype": "success", "request_id": p["request_id"],
                                 "response": self.responses.get(sub, {})}
                        if sub == "initialize":
                            inner.update(pending_permission_requests=[], pending_user_dialog_requests=[])
                    self.log.append({"sequence_num": str(len(self.log) + 1), "source": "worker",
                                     "payload": {"type": "control_response", "response": inner}})
            return httpx.Response(200, json={"results": results, "attestation_feedback": None})
        if req.method == "GET" and req.url.path.endswith("/events"):
            cursor = int(req.url.params.get("cursor") or 0)
            rows = [r for r in self.log if int(r["sequence_num"]) > cursor]
            if req.url.params.get("sort_order") == "desc":
                rows = list(reversed(self.log))
            return httpx.Response(200, json={"data": rows[: int(req.url.params.get("limit") or 50)]})
        return httpx.Response(404, text="unexpected")

    def last_request(self) -> dict:
        return [p for p in self.posted if p["type"] == "control_request"][-1]["request"]


def _client(handler) -> RemoteControlClient:
    return RemoteControlClient(token="tok", org_uuid="org-1", transport=httpx.MockTransport(handler))


# --- chat ----------------------------------------------------------------------
def test_send_prompt_matches_web_shape_and_options():
    fake = FakeSession()
    with _client(fake.handler) as rc:
        rc.send_prompt(SID, "hi this is a test")
        rc.send_prompt(SID, "look", images=[(b"\x89PNG", "image/png")],
                       file_attachments=[{"file_uuid": "f1", "file_name": "a.txt"}],
                       priority="next", requeue_if_interrupted_within_ms=3000, message_uuid="m-1")
        rc.queue_message(SID, "later please")
    plain, rich, queued = fake.posted
    assert plain["message"] == {"role": "user", "content": "hi this is a test"}
    assert plain["session_id"] == SID and plain["parent_tool_use_id"] is None and plain["uuid"]
    assert "priority" not in plain
    assert rich["uuid"] == "m-1" and rich["priority"] == "next"
    assert rich["requeue_if_interrupted_within_ms"] == 3000
    assert rich["file_attachments"] == [{"file_uuid": "f1", "file_name": "a.txt"}]
    assert rich["message"]["content"][0] == {"type": "text", "text": "look"}
    assert rich["message"]["content"][1] == image_block(b"\x89PNG", "image/png")
    assert rich["message"]["content"][1]["source"]["data"] == "iVBORw=="
    assert queued["priority"] == "later"
    req = fake.requests[0]
    assert req.headers["anthropic-client-platform"] == "web_claude_ai"


def test_send_prompt_validates_priority():
    with _client(FakeSession().handler) as rc:
        with pytest.raises(ValueError):
            rc.send_prompt(SID, "x", priority="urgent")
        with pytest.raises(ValueError):
            rc.send_prompt(SID, "x", requeue_if_interrupted_within_ms=20000)


def test_interrupt_variants():
    fake = FakeSession(responses={"interrupt": {"still_queued": [], "turn": "stopped"}})
    with _client(fake.handler) as rc:
        rc.stop_turn(SID)
        assert fake.last_request() == {"subtype": "interrupt"}
        rc.cancel_queued(SID)
        assert fake.last_request() == {"subtype": "interrupt", "cancel_queued": True}
        rc.send_queued_now(SID, "m-1")
        assert fake.last_request() == {"subtype": "interrupt", "send_now": True, "message_uuid": "m-1"}
        receipt = rc.interrupt_turn(SID, reason="user", timeout=5)
        assert receipt == {"still_queued": [], "turn": "stopped"}
        with pytest.raises(ValueError):
            rc.interrupt_turn(SID, for_user_message_uuids=[str(i) for i in range(9)])


# --- controls --------------------------------------------------------------------
@pytest.mark.parametrize("call, subtype, fields", [
    (lambda rc: rc.set_max_thinking_tokens(SID, 4096, thinking_display="summarized"),
     "set_max_thinking_tokens", {"max_thinking_tokens": 4096, "thinking_display": "summarized"}),
    (lambda rc: rc.set_max_thinking_tokens(SID, None), "set_max_thinking_tokens", {"max_thinking_tokens": None}),
    (lambda rc: rc.set_fast_mode(SID, True), "apply_flag_settings", {"settings": {"fastMode": True}}),
    (lambda rc: rc.set_advisor_model(SID, "claude-opus-5-5"), "apply_flag_settings",
     {"settings": {"advisorModel": "claude-opus-5-5"}}),
    (lambda rc: rc.set_always_thinking(SID, False), "apply_flag_settings",
     {"settings": {"alwaysThinkingEnabled": False}}),
    (lambda rc: rc.set_view_mode(SID, "verbose"), "apply_flag_settings", {"settings": {"viewMode": "verbose"}}),
    (lambda rc: rc.set_worker_title(SID, "New"), "rename_session", {"title": "New", "source": "remote"}),
    (lambda rc: rc.rewind_conversation(SID, "u-1", allow_assistant_target=True), "rewind_conversation",
     {"target_message_uuid": "u-1", "allow_assistant_target": True}),
    (lambda rc: rc.mcp_toggle(SID, "github", False), "mcp_toggle", {"serverName": "github", "enabled": False}),
    (lambda rc: rc.mcp_reconnect(SID, "github"), "mcp_reconnect", {"serverName": "github"}),
    (lambda rc: rc.set_mcp_permission_mode_override(SID, "gh", "auto"), "set_mcp_permission_mode_override",
     {"serverName": "gh", "mode": "auto"}),
    (lambda rc: rc.mcp_call(SID, "mcp__gh__x", {"a": 1}), "mcp_call", {"tool": "mcp__gh__x", "arguments": {"a": 1}}),
    (lambda rc: rc.reload_skills(SID), "reload_skills", {}),
    (lambda rc: rc.reload_plugins(SID, hold_on_cache_impact=True), "reload_plugins", {"hold_on_cache_impact": True}),
    (lambda rc: rc.reload_output_styles(SID), "reload_output_styles", {}),
    (lambda rc: rc.get_session_cost(SID), "get_session_cost", {}),
    (lambda rc: rc.list_models(SID), "list_models", {}),
    (lambda rc: rc.get_binary_version(SID), "get_binary_version", {}),
    (lambda rc: rc.get_settings(SID), "get_settings", {}),
    (lambda rc: rc.get_hooks_listing(SID), "get_hooks_listing", {}),
    (lambda rc: rc.list_permission_rules(SID), "list_permission_rules", {}),
    (lambda rc: rc.get_plan(SID), "get_plan", {}),
    (lambda rc: rc.read_file(SID, "a.txt", max_bytes=10), "read_file", {"path": "a.txt", "max_bytes": 10}),
    (lambda rc: rc.list_directory(SID, "src"), "list_directory", {"path": "src"}),
    (lambda rc: rc.seed_read_state(SID, "a", 1.5), "seed_read_state", {"path": "a", "mtime": 1.5}),
    (lambda rc: rc.background_tasks(SID, "tu-1"), "background_tasks", {"tool_use_id": "tu-1"}),
    (lambda rc: rc.get_task_output(SID, "t-1"), "get_task_output", {"task_id": "t-1"}),
    (lambda rc: rc.send_task_message(SID, "t-1", "hey"), "send_task_message", {"task_id": "t-1", "message": "hey"}),
    (lambda rc: rc.start_shell_task(SID, "npm", args=["run", "dev"]), "start_shell_task",
     {"command": "npm", "args": ["run", "dev"]}),
    (lambda rc: rc.submit_feedback(SID, "great", title="t"), "submit_feedback",
     {"description": "great", "surface": "ccr", "title": "t"}),
    (lambda rc: rc.message_rated(SID, "m-1", "positive"), "message_rated",
     {"messageUuid": "m-1", "sentiment": "positive", "cleared": False}),
    (lambda rc: rc.set_prompt_suggestions_paused(SID, True), "set_prompt_suggestions_paused", {"paused": True}),
    (lambda rc: rc.end_session(SID, "done"), "end_session", {"reason": "done"}),
])
def test_typed_controls_send_their_subtype(call, subtype, fields):
    fake = FakeSession()
    with _client(fake.handler) as rc:
        call(rc)
    assert fake.last_request() == {"subtype": subtype, **fields}


def test_control_return_values_and_errors():
    fake = FakeSession(
        responses={"file_suggestions": {"suggestions": [{"path": "src/a.py"}, {"path": "b.md"}]},
                   "mcp_status": {"mcpServers": [{"name": "gh", "status": "connected"}]},
                   "generate_session_title": {"title": "Fix tests"},
                   "get_task_output": {"output": "ok", "total_bytes": 2, "truncated": False}},
        errors={"list_directory": "list_directory is not available"},
    )
    with _client(fake.handler) as rc:
        assert rc.file_suggestions(SID, "a") == ["src/a.py", "b.md"]
        assert rc.mcp_status(SID) == [{"name": "gh", "status": "connected"}]
        assert rc.generate_session_title(SID, "fix the tests") == "Fix tests"
        assert fake.last_request() == {"subtype": "generate_session_title", "description": "fix the tests"}
        assert rc.get_task_output(SID, "t")["output"] == "ok"
        with pytest.raises(ControlRejected):
            rc.list_directory(SID, "/")


def test_apply_flag_settings_checks_keys_locally():
    fake = FakeSession()
    with _client(fake.handler) as rc:
        with pytest.raises(ValueError):
            rc.apply_flag_settings(SID, {"theme": "dark"})
        with pytest.raises(ValueError):
            rc.apply_flag_settings(SID, {"fastMode": True}, allowed=inter.RC_FLAG_SETTINGS)
        rc.apply_flag_settings(SID, {"effortLevel": "high", "ultracode": False}, allowed=inter.RC_FLAG_SETTINGS)
        rc.apply_flag_settings(SID, {"anything": 1}, allowed=None)
    assert fake.posted and len([p for p in fake.posted if p["type"] == "control_request"]) == 2
    assert {"model", "advisorModel", "effortLevel", "ultracode", "fastMode", "viewMode",
            "alwaysThinkingEnabled"} == CLOUD_FLAG_SETTINGS


def test_end_session_fire_and_forget_returns_ack():
    fake = FakeSession()
    with _client(fake.handler) as rc:
        ack = rc.end_session(SID)
    assert ack["results"][0]["sequence_num"] == "1"
    assert fake.last_request() == {"subtype": "end_session"}


# --- prompts ---------------------------------------------------------------------
PERMISSION = {
    "type": "control_request", "request_id": "perm-1",
    "request": {"subtype": "can_use_tool", "tool_name": "Write", "tool_use_id": "tu-9",
                "input": {"file_path": "a.txt", "content": "x"},
                "permission_suggestions": [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]},
}
QUESTION = {
    "type": "control_request", "request_id": "q-1",
    "request": {"subtype": "can_use_tool", "tool_name": "AskUserQuestion", "tool_use_id": "tu-q",
                "input": {"questions": [
                    {"question": "Do you prefer red or blue?", "header": "Color", "multiSelect": False,
                     "options": [{"label": "Red", "description": "Choose red."},
                                 {"label": "Blue", "description": "Choose blue."}]},
                    {"question": "Which toppings?", "header": "Toppings", "multiSelect": True,
                     "options": [{"label": "Cheese", "description": ""}, {"label": "Ham, smoked", "description": ""},
                                 {"label": "Olives", "description": ""}]},
                ]}},
}


def _ev(seq, payload, source="worker"):
    return Event.from_wire({"sequence_num": str(seq), "source": source, "payload": payload})


def _response_of(fake):
    return fake.posted[-1]["response"]


def test_allow_once_always_allow_and_deny():
    fake = FakeSession()
    pr = PendingRequest.from_payload(PERMISSION)
    assert pr.kind == "permission" and pr.tool_name == "Write"
    with _client(fake.handler) as rc:
        rc.allow_once(SID, pr)
        r = _response_of(fake)
        assert r["subtype"] == "success" and r["request_id"] == "perm-1"
        assert r["response"] == {"behavior": "allow", "updatedInput": PERMISSION["request"]["input"],
                                 "tool_name": "Write", "toolUseID": "tu-9",
                                 "decisionClassification": "user_temporary"}
        rc.always_allow(SID, pr)
        r = _response_of(fake)["response"]
        assert r["updatedPermissions"] == PERMISSION["request"]["permission_suggestions"]
        assert r["decisionClassification"] == "user_permanent"
        rc.deny_permission(SID, pr, "no thanks", interrupt=True)
        r = _response_of(fake)["response"]
        assert r == {"behavior": "deny", "message": "no thanks", "interrupt": True, "toolUseID": "tu-9",
                     "decisionClassification": "user_reject"}
        rc.allow_once(SID, "bare-id")
        assert _response_of(fake)["response"] == {"behavior": "allow", "decisionClassification": "user_temporary"}
        with pytest.raises(ValueError):
            rc.always_allow(SID, "bare-id")


def test_dialog_elicitation_and_hook_answers():
    fake = FakeSession()
    with _client(fake.handler) as rc:
        rc.answer_dialog(SID, "d-1", result={"choice": "ok"})
        assert _response_of(fake) == {"subtype": "success", "request_id": "d-1",
                                      "response": {"behavior": "completed", "result": {"choice": "ok"}}}
        rc.answer_dialog(SID, "d-2", completed=False)
        assert _response_of(fake)["response"] == {"behavior": "cancelled"}
        rc.answer_elicitation(SID, "e-1", "accept", {"name": "x"})
        assert _response_of(fake)["response"] == {"action": "accept", "content": {"name": "x"}}
        rc.answer_elicitation(SID, "e-2", "decline")
        assert _response_of(fake)["response"] == {"action": "decline"}
        with pytest.raises(ValueError):
            rc.answer_elicitation(SID, "e-3", "maybe")
        rc.answer_hook_callback(SID, "h-1", {"continue": True, "decision": "approve"})
        assert _response_of(fake)["response"] == {"continue": True, "decision": "approve"}
    assert cli_user_dialog_response("x", completed=True)["response"]["response"] == {"behavior": "completed"}


# --- AskUserQuestion ---------------------------------------------------------------
def test_question_parsing():
    pr = PendingRequest.from_payload(QUESTION)
    assert pr.kind == "question"
    q0, q1 = pr.questions
    assert (q0.question, q0.header, q0.multi_select, q0.labels) == (
        "Do you prefer red or blue?", "Color", False, ["Red", "Blue"])
    assert q0.options[1].description == "Choose blue."
    assert q1.multi_select and q1.labels == ["Cheese", "Ham, smoked", "Olives"]
    assert PendingRequest.from_payload(PERMISSION).questions == []


def test_build_answers_single_multi_other_and_errors():
    qs = PendingRequest.from_payload(QUESTION).questions
    assert build_answers(qs, {0: "Blue", "Toppings": ["Cheese", "Olives"]}) == {
        "Do you prefer red or blue?": "Blue", "Which toppings?": ["Cheese", "Olives"]}
    assert build_answers(qs, {"Which toppings?": "Cheese"}, other={1: "Pineapple"}) == {
        "Which toppings?": ["Cheese", "Pineapple"]}
    assert build_answers(qs, other={"Color": "Green"}) == {"Do you prefer red or blue?": "Green"}
    with pytest.raises(ValueError):
        build_answers(qs, {0: "Purple"})
    with pytest.raises(ValueError):
        build_answers(qs, {0: ["Red", "Blue"]})
    with pytest.raises(ValueError):
        build_answers(qs, {0: "Red"}, other={0: "Green"})
    with pytest.raises(KeyError):
        build_answers(qs, {"Nope": "Red"})


def test_answer_question_helpers_send_updated_input():
    fake = FakeSession()
    pr = PendingRequest.from_payload(QUESTION)
    original = QUESTION["request"]["input"]
    with _client(fake.handler) as rc:
        rc.select_option(SID, pr, "Blue")
        r = _response_of(fake)["response"]
        assert r["behavior"] == "allow" and r["tool_name"] == "AskUserQuestion" and r["toolUseID"] == "tu-q"
        assert r["updatedInput"]["questions"] == original["questions"]
        assert r["updatedInput"]["answers"] == {"Do you prefer red or blue?": "Blue"}
        assert "annotations" not in r["updatedInput"]

        rc.select_options(SID, pr, ["Cheese", "Ham, smoked"], question="Toppings", other="Basil")
        assert _response_of(fake)["response"]["updatedInput"]["answers"] == {
            "Which toppings?": ["Cheese", "Ham, smoked", "Basil"]}

        rc.answer_other(SID, pr, "Green")
        assert _response_of(fake)["response"]["updatedInput"]["answers"] == {"Do you prefer red or blue?": "Green"}

        rc.answer_questions(SID, pr, {0: "Red"}, skipped=[1], notes={0: "brighter"})
        inp = _response_of(fake)["response"]["updatedInput"]
        assert inp["answers"] == {"Do you prefer red or blue?": "Red"}
        assert inp["annotations"] == {"Which toppings?": {"unanswered": "skipped"},
                                      "Do you prefer red or blue?": {"notes": "brighter"}}

        rc.skip_questions(SID, pr)
        inp = _response_of(fake)["response"]["updatedInput"]
        assert inp["answers"] == {}
        assert inp["annotations"] == {q["question"]: {"unanswered": "skipped"} for q in original["questions"]}

        with pytest.raises(ValueError):
            rc.answer_questions(SID, PendingRequest.from_payload(PERMISSION), {0: "x"})
    assert "answers" not in original  # the request's own input is never mutated


def test_join_answer_labels_matches_worker_encoding():
    assert join_answer_labels(["Cheese", "Olives"]) == "Cheese, Olives"
    assert join_answer_labels(["Ham, smoked", 'say "hi"']) == '"Ham, smoked", "say \\"hi\\""'
    out = cli_question_answer("q", {"questions": []}, {"a": "b"}, annotations={"a": {"notes": "n"}})
    assert out["response"]["response"]["updatedInput"] == {"questions": [], "answers": {"a": "b"},
                                                           "annotations": {"a": {"notes": "n"}}}


# --- pending requests ---------------------------------------------------------------
def test_find_pending_tracks_answers_and_cancels():
    elicit = {"type": "control_request", "request_id": "el-1",
              "request": {"subtype": "elicitation", "mcp_server_name": "gh", "message": "Name?"}}
    own = {"type": "control_request", "request_id": "init-1", "request": {"subtype": "can_use_tool"}}
    events = [
        _ev(1, PERMISSION), _ev(2, QUESTION), _ev(3, elicit), _ev(4, own, source="client"),
        _ev(5, {"type": "control_response", "response": {"subtype": "success", "request_id": "perm-1",
                                                         "response": {"behavior": "allow"}}}, source="client"),
        _ev(6, {"type": "control_cancel_request", "request_id": "el-1"}),
        _ev(7, {"type": "assistant", "message": {"role": "assistant", "content": "hi"}}),
    ]
    pending = find_pending(events)
    assert [p.request_id for p in pending] == ["q-1"]
    assert pending[0].kind == "question" and pending[0].sequence_num == 2


def test_pending_requests_reads_newest_history():
    fake = FakeSession()
    fake.log = [{"sequence_num": "1", "source": "worker", "payload": PERMISSION},
                {"sequence_num": "2", "source": "worker", "payload": {
                    "type": "control_request", "request_id": "dlg-1",
                    "request": {"subtype": "request_user_dialog", "dialog_kind": "x", "payload": {}}}}]
    with _client(fake.handler) as rc:
        pending = rc.pending_requests(SID)
    assert [(p.request_id, p.kind) for p in pending] == [("perm-1", "permission"), ("dlg-1", "dialog")]
    assert fake.requests[0].url.params["sort_order"] == "desc"


def test_pending_from_initialize():
    init = {"pending_permission_requests": [PERMISSION], "pending_user_dialog_requests": [{"bad": 1}]}
    assert [p.request_id for p in pending_from_initialize(init)] == ["perm-1"]


# --- slash commands ------------------------------------------------------------------
def test_list_and_run_commands():
    commands = [{"name": "startup-hook-skill", "aliases": ["session-start-hook"], "argumentHint": "",
                 "description": "Hooks (user)"},
                {"name": "design", "argumentHint": "[what to design]", "builtin": True, "description": "Design"}]
    fake = FakeSession(responses={"initialize": {"commands": commands, "claude_code_version": "2.1.312"}})
    with _client(fake.handler) as rc:
        cmds = rc.list_commands(SID)
        assert [c.name for c in cmds] == ["startup-hook-skill", "design"]
        assert cmds[0].aliases == ["session-start-hook"] and cmds[1].builtin
        assert cmds[1].argument_hint == "[what to design]"
        rc.run_command(SID, "/design", "a landing page")
        assert fake.posted[-1]["message"]["content"] == "/design a landing page"
        rc.run_command(SID, "compact")
        assert fake.posted[-1]["message"]["content"] == "/compact"
        with pytest.raises(ValueError):
            rc.run_command(SID, "theme", strict=True)
    assert is_client_only("/theme") and is_client_only("login")
    assert not is_client_only("model") and not is_client_only("design")


# --- session stream ---------------------------------------------------------------------
STREAM = (
    b": keepalive\n\n"
    b"event: client_event\nid: 4\ndata: {\"sequence_num\":\"4\",\"source\":\"worker\",\"payload\":"
    b"{\"type\":\"assistant\",\"message\":{\"role\":\"assistant\",\"content\":\"hi\"}}}\n\n"
    b"event: client_event\nid: 4\ndata: {\"sequence_num\":\"4\",\"source\":\"worker\",\"payload\":"
    b"{\"type\":\"assistant\",\"message\":{\"role\":\"assistant\",\"content\":\"hi\"}}}\n\n"
    b"event: ephemeral_event\ndata: {\"event_type\":\"stream_event\",\"source\":\"worker\","
    b"\"payload\":{\"type\":\"stream_event\",\"event\":{\"type\":\"message_start\"}}}\n\n"
    b"event: session_update\ndata: {\"connection_status\":\"connected\"}\n\n"
    b"event: security_update\ndata: {\"security_tier\":\"elevated\"}\n\n"
    b"event: delivery_update\ndata: {\"event_id\":\"e1\",\"status\":\"DELIVERY_STATUS_PROCESSED\"}\n\n"
    b"event: catch_up_truncated\ndata: {}\n\n"
)


def test_stream_session_yields_meta_frames_and_control_only():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})

    with _client(handler) as rc:
        events = list(rc.stream_control(SID, from_sequence_num=3, reconnect=False))
    assert seen[0].url.params["control_only"] == "1"
    assert seen[0].url.params["from_sequence_num"] == "3"
    assert seen[0].headers["Last-Event-ID"] == "3"
    assert [e.type for e in events] == [
        "assistant", "stream_event", StreamFrame.SESSION_UPDATE, StreamFrame.SECURITY_UPDATE,
        StreamFrame.DELIVERY_UPDATE, StreamFrame.CATCH_UP_TRUNCATED]
    assert events[0].sequence_num == 4 and events[1].sequence_num is None
    assert events[2].payload["connection_status"] == "connected"
    assert events[3].payload["security_tier"] == "elevated"
    assert events[4].raw == {"event": "delivery_update",
                             "data": {"event_id": "e1", "status": "DELIVERY_STATUS_PROCESSED"}}


def test_stream_session_reconnects_from_last_sequence():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(200, content=STREAM[:260])
        raise httpx.ConnectError("down")

    with _client(handler) as rc:
        gen = rc.stream_session(SID, max_reconnects=1)
        first = next(gen)
        with pytest.raises(httpx.ConnectError):
            list(gen)
    assert first.sequence_num == 4
    assert "control_only" not in calls[0].url.params
    assert calls[1].url.params["from_sequence_num"] == "4" and calls[1].headers["Last-Event-ID"] == "4"


# --- account watch -----------------------------------------------------------------------
WATCH_BODY = (
    b"event: added\ndata: {\"id\":\"session_a\",\"title\":\"A\",\"status\":\"active\"}\n\n"
    b": ka\n\n"
    b"event: sync\nid: tok-2\ndata: {}\n\n"
    b"event: changed\ndata: {\"id\":\"session_a\",\"title\":\"A2\",\"worker_status\":\"running\"}\n\n"
    b"event: removed\ndata: {\"id\":\"session_b\"}\n\n"
)


def test_watch_mints_token_and_parses_frames():
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/v1/code/sessions":
            return httpx.Response(200, json={"data": [], "resume_token": "tok-1"})
        return httpx.Response(200, content=WATCH_BODY)

    with _client(handler) as rc:
        frames = list(rc.watch(reconnect=False))
    assert seen[0].url.params["limit"] == "1"
    w = seen[1]
    assert w.url.path == "/v1/code/sessions/watch"
    assert w.url.params["resume_token"] == "tok-1" and w.headers["Last-Event-ID"] == "tok-1"
    assert w.url.params["exclude_tags"] == "-" and w.url.params["include_trigger_sessions"] == "true"
    assert w.url.params["keepalive_tolerance_seconds"] == "135"
    assert w.headers["anthropic-client-platform"] == "web_claude_ai"
    assert w.headers["Accept"] == "text/event-stream"
    assert [(f.kind, f.session_id, f.resume_token) for f in frames] == [
        ("added", "session_a", None), ("sync", None, "tok-2"), ("changed", "session_a", None),
        ("removed", "session_b", None)]
    assert frames[2].session["title"] == "A2"


def test_watch_resumes_with_sync_token_and_remints_when_stale(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(inter.time, "monotonic", lambda: clock[0])
    watch_calls = []
    mints = []

    def handler(req):
        if req.url.path == "/v1/code/sessions":
            mints.append(1)
            return httpx.Response(200, json={"data": [], "resume_token": f"mint-{len(mints)}"})
        watch_calls.append(req.headers["Last-Event-ID"])
        n = len(watch_calls)
        if n == 1:
            return httpx.Response(200, content=b"event: sync\nid: tok-2\ndata: {}\n\n")
        if n == 2:
            clock[0] += 300  # the connection idled past the token's life
            raise httpx.ReadTimeout("idle")
        if n == 3:
            return httpx.Response(200, content=b"event: changed\ndata: {\"id\":\"s\"}\n\n")
        raise httpx.ConnectError("stop")

    with _client(handler) as rc:
        out = []
        with pytest.raises(httpx.ConnectError):
            for f in rc.watch(resume_token="given", max_reconnects=2):
                out.append(f)
    assert watch_calls[:3] == ["given", "tok-2", "mint-1"]
    assert [f.kind for f in out] == ["sync", "resync", "changed"]
    assert out[1] == WatchEvent("resync", resume_token="mint-1")


def test_watch_410_right_after_mint_ends_and_404_raises():
    def gone(req):
        if req.url.path == "/v1/code/sessions":
            return httpx.Response(200, json={"resume_token": "t"})
        return httpx.Response(410, json={"type": "error", "error": {"type": "gone"}})

    with _client(gone) as rc:
        assert list(rc.watch()) == []

    def not_enabled(req):
        return httpx.Response(404, text="endpoint not enabled")

    from claude_rc.client import APIError
    with _client(not_enabled) as rc:
        with pytest.raises(APIError):
            list(rc.watch(resume_token="t"))
