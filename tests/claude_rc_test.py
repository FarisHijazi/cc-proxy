"""Offline tests for the reverse-engineered client (no network)."""
import json
from pathlib import Path

import pytest

from claude_rc.sse import parse_sse, SSEFrame
from claude_rc.events import (
    Event,
    cli_user_message,
    cli_control_request,
    user_message,
)
from claude_rc.credentials import OAuthCredentials, load_credentials, CredentialsError


# --- SSE parser ------------------------------------------------------------
def test_sse_basic_frame():
    lines = ["event: client_event", "id: 42", 'data: {"a":1}', ""]
    frames = list(parse_sse(lines))
    assert len(frames) == 1
    assert frames[0].event == "client_event"
    assert frames[0].id == "42"
    assert json.loads(frames[0].data) == {"a": 1}


def test_sse_multiline_data_and_heartbeat():
    lines = ["data: line1", "data: line2", "", ": keep-alive", ""]
    frames = list(parse_sse(lines))
    # comment-only block yields no frame; the data block joins with newline
    assert len(frames) == 1
    assert frames[0].data == "line1\nline2"
    assert frames[0].event is None


def test_sse_strips_single_leading_space():
    frames = list(parse_sse(["data:  two-spaces", ""]))
    assert frames[0].data == " two-spaces"  # only the first space is stripped


# --- Event model -----------------------------------------------------------
def test_event_rc_wrapped_unwraps_payload_and_int_seq():
    wire = {"sequence_num": "362", "payload": {"type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}}
    e = Event.from_wire(wire)
    assert e.type == "assistant"
    assert e.role == "assistant"
    assert e.sequence_num == 362 and isinstance(e.sequence_num, int)
    assert e.text() == "hi"


def test_event_rc_string_content():
    wire = {"sequence_num": 1, "payload": {"type": "user",
            "message": {"role": "user", "content": "just a string"}}}
    e = Event.from_wire(wire)
    assert e.text() == "just a string"


def test_event_managed_agents_shape():
    wire = {"type": "agent.message", "id": "sevt_1",
            "content": [{"type": "text", "text": "cloud"}], "processed_at": "2026-01-01T00:00:00Z"}
    e = Event.from_wire(wire)
    assert e.type == "agent.message"
    assert e.text() == "cloud"
    assert e.sequence_num is None


def test_event_turn_end_and_terminal():
    assert Event.from_wire({"payload": {"type": "result", "subtype": "success"}}).is_turn_end
    assert Event.from_wire({"payload": {"type": "result", "subtype": "error_max_turns"}}).is_terminal
    assert Event.from_wire({"type": "session.status_idle"}).is_turn_end


def test_event_blocking_control():
    e = Event.from_wire({"payload": {"type": "control_request",
                                     "request": {"subtype": "can_use_tool"}}})
    assert e.is_blocking_control


def test_event_tool_uses():
    e = Event.from_wire({"payload": {"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}, "id": "toolu_1"}]}}})
    tus = e.tool_uses()
    assert len(tus) == 1 and tus[0]["name"] == "Bash"


# --- builders --------------------------------------------------------------
def test_cli_user_message_shape():
    m = cli_user_message("hello")
    assert m["type"] == "user"
    assert m["message"] == {"role": "user", "content": [{"type": "text", "text": "hello"}]}


def test_cli_control_request_shape():
    r = cli_control_request("set_model", "rid-1", model="claude-opus-5-5")
    assert r["type"] == "control_request"
    assert r["request"] == {"subtype": "set_model", "model": "claude-opus-5-5"}


def test_cli_control_request_effort_shape():
    # Effort rides apply_flag_settings (there is no set_effort subtype); an
    # explicit null effortLevel means "clear back to auto", so it must survive.
    r = cli_control_request(
        "apply_flag_settings", "rid-2", settings={"effortLevel": None, "ultracode": False}
    )
    assert r["request"] == {
        "subtype": "apply_flag_settings",
        "settings": {"effortLevel": None, "ultracode": False},
    }


# --- set_effort verdict handling (offline: transport methods stubbed) -------
def _bare_rc() -> "RemoteControlClient":
    from claude_rc.client import RemoteControlClient

    return RemoteControlClient.__new__(RemoteControlClient)  # skip __init__ / creds


def test_set_effort_control_accepted(monkeypatch):
    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or {"ok": 1})
    monkeypatch.setattr(
        rc, "wait_control_response", lambda sid, rid, **kw: {"subtype": "success", "request_id": rid}
    )
    out = rc.set_effort("cse_x", "high", wait=1.0, command_fallback=True)
    assert out["via"] == "control"
    assert sent[0][0]["request"]["settings"] == {"effortLevel": "high", "ultracode": False}


def test_set_effort_command_fallback(monkeypatch):
    # Remote-control REPL workers refuse apply_flag_settings — the fallback
    # injects the /effort slash command (which they execute as a local command).
    rc = _bare_rc()
    messages = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(
        rc,
        "wait_control_response",
        lambda sid, rid, **kw: {
            "subtype": "error",
            "error": "REPL bridge does not handle control_request subtype: apply_flag_settings",
            "request_id": rid,
        },
    )
    monkeypatch.setattr(rc, "send_message", lambda sid, text, **kw: messages.append(text) or {})
    assert rc.set_effort("cse_x", "high", wait=1.0, command_fallback=True)["via"] == "command"
    assert messages == ["/effort high"]
    assert rc.set_effort("cse_x", None, wait=1.0, command_fallback=True)["via"] == "command"
    assert messages[-1] == "/effort auto"


def test_set_effort_rejected_without_fallback(monkeypatch):
    from claude_rc.client import ControlRejected

    rc = _bare_rc()
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(
        rc, "wait_control_response", lambda sid, rid, **kw: {"subtype": "error", "error": "nope"}
    )
    try:
        rc.set_effort("cse_x", "low", wait=1.0)
        raise AssertionError("expected ControlRejected")
    except ControlRejected as exc:
        assert "nope" in str(exc)


def test_set_effort_timeout_no_fallback(monkeypatch):
    # No verdict within `wait` → assume applied, do NOT double-apply via command.
    rc = _bare_rc()
    messages = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: {})
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: None)
    monkeypatch.setattr(rc, "send_message", lambda sid, text, **kw: messages.append(text) or {})
    assert rc.set_effort("cse_x", "high", wait=0.1, command_fallback=True)["via"] == "control_unconfirmed"
    assert messages == []


def test_managed_agents_user_message_shape():
    assert user_message("x") == {"type": "user.message", "content": [{"type": "text", "text": "x"}]}


def test_managed_agents_stream_raises_apierror_on_non_200(monkeypatch):
    """A failed stream must surface as APIError.

    The body has to be read before it is formatted: httpx raises
    ``ResponseNotRead`` if ``.text`` is touched on a still-streaming response,
    which would mask the real status behind an unrelated exception.
    """
    from claude_rc.client import APIError, ManagedAgentsClient

    class _Resp:
        status_code = 503
        headers = {"request-id": "req_1"}
        read_called = False

        def read(self):
            type(self).read_called = True
            return b"upstream unavailable"

        @property
        def text(self):
            raise AssertionError("must not read .text of an unread stream")

        def iter_lines(self):
            raise AssertionError("must not parse the body of an error response")

    class _Stream:
        def __enter__(self):
            return _Resp()

        def __exit__(self, *exc):
            return False

    ma = ManagedAgentsClient(api_key="k")
    monkeypatch.setattr(ma._http, "stream", lambda *a, **kw: _Stream())
    with pytest.raises(APIError) as excinfo:
        list(ma.stream_events("sesn_1"))
    assert excinfo.value.status == 503
    assert excinfo.value.request_id == "req_1"
    assert "upstream unavailable" in excinfo.value.body
    assert _Resp.read_called
    ma.close()


# --- credentials -----------------------------------------------------------
def test_load_credentials_from_file(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a" * 108, "refreshToken": "r" * 108,
        "expiresAt": 9999999999999, "scopes": ["user:inference"],
        "subscriptionType": "max", "clientId": "cid"}}))
    c = load_credentials(p)
    assert c.access_token == "a" * 108
    assert c.client_id == "cid"
    assert not c.is_expired()


def test_load_credentials_expired(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1}}))
    assert load_credentials(p).is_expired()


def test_load_credentials_missing_block(tmp_path: Path):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"somethingElse": {}}))
    with pytest.raises(CredentialsError):
        load_credentials(p)


# --- rotated-token recovery (long-lived clients) -----------------------------
def _write_creds(p: Path, token: str, refresh: str, expires_at_ms: int) -> None:
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": token, "refreshToken": refresh,
        "expiresAt": expires_at_ms, "scopes": ["user:inference"],
        "subscriptionType": "max"}}))


def _client_for(p: Path):
    from claude_rc.client import RemoteControlClient
    return RemoteControlClient(credentials_path=str(p), org_uuid="org-x")


def test_token_prefers_rotated_disk_copy(tmp_path: Path, monkeypatch):
    """Expired in memory + fresh tokens on disk (another process refreshed)
    → take the disk copy; never spend our own (already-dead) refresh token."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)  # long expired
    client = _client_for(p)

    def _no_refresh(*a, **kw):
        raise AssertionError("network refresh must not run when disk has fresh tokens")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _no_refresh)
    _write_creds(p, "new-token", "new-refresh", 9999999999999)  # rotated by "someone else"
    assert client._token() == "new-token"
    client.close()


def test_token_reloads_disk_after_invalid_grant(tmp_path: Path, monkeypatch):
    """Our refresh attempt fails (single-use token already spent) but fresh
    tokens landed on disk meanwhile → recover from disk instead of raising."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    client = _client_for(p)

    def _invalid_grant(*a, **kw):
        _write_creds(p, "new-token", "new-refresh", 9999999999999)
        raise CredentialsError("token refresh failed: HTTP 400 invalid_grant")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _invalid_grant)
    assert client._token() == "new-token"
    client.close()


def test_token_raises_when_no_recovery_possible(tmp_path: Path, monkeypatch):
    """Refresh fails and the disk still holds the same dead tokens → raise."""
    p = tmp_path / ".credentials.json"
    _write_creds(p, "old-token", "old-refresh", 1)
    client = _client_for(p)

    def _invalid_grant(*a, **kw):
        raise CredentialsError("token refresh failed: HTTP 400 invalid_grant")

    monkeypatch.setattr("claude_rc.client.refresh_credentials", _invalid_grant)
    with pytest.raises(CredentialsError):
        client._token()
    client.close()


# --- envelope + control-protocol accessors -----------------------------------
def _wire(payload, **envelope):
    return {"sequence_num": "7", "payload": payload, **envelope}


def test_event_envelope_accessors():
    e = Event.from_wire(_wire(
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        event_id="ev-1", source="client", created_at="2026-10-16T15:17:54Z",
        device_attestation_status="DEVICE_ATTESTATION_STATUS_UNSPECIFIED",
    ))
    assert (e.event_id, e.source, e.created_at) == ("ev-1", "client", "2026-10-16T15:17:54Z")
    assert e.attestation == "UNSPECIFIED"
    assert Event.from_wire({"type": "agent.message"}).attestation is None


def test_blocking_control_is_worker_prompts_only():
    def req(subtype, source):
        return Event.from_wire(_wire(
            {"type": "control_request", "request_id": "r1", "request": {"subtype": subtype}}, source=source
        ))

    assert req("can_use_tool", "worker").is_blocking_control
    assert req("request_user_dialog", "worker").is_blocking_control
    assert req("can_use_tool", None).is_blocking_control  # envelope without a source
    # A controller's side question (ours, or claude.ai's "btw") is not a prompt.
    assert not req("side_question", "client").is_blocking_control
    assert not req("side_question", "worker").is_blocking_control
    assert not req("can_use_tool", "client").is_blocking_control
    assert not req("interrupt", "client").is_blocking_control


def test_request_id_and_control_subtype():
    ask = Event.from_wire(_wire({"type": "control_request", "request_id": "r1", "request": {"subtype": "get_usage"}}))
    ans = Event.from_wire(_wire({"type": "control_response", "response": {"subtype": "success", "request_id": "r1"}}))
    assert (ask.request_id, ask.control_subtype) == ("r1", "get_usage")
    assert (ans.request_id, ans.control_subtype) == ("r1", None)
    assert Event.from_wire(_wire({"type": "assistant"})).request_id is None


def test_worker_state():
    init = Event.from_wire(_wire({
        "type": "system", "subtype": "init", "model": "claude-haiku-5-5", "effort": "max",
        "permissionMode": "bypassPermissions", "claude_code_version": "2.1.301", "cwd": "",
    }))
    assert init.worker_state == {
        "model": "claude-haiku-5-5", "effort": "max",
        "permission_mode": "bypassPermissions", "claude_code_version": "2.1.301",
    }
    status = Event.from_wire(_wire({"type": "system", "subtype": "status", "permissionMode": "plan", "status": None}))
    assert status.worker_state == {"permission_mode": "plan"}
    assert Event.from_wire(_wire({"type": "system", "subtype": "task_started"})).worker_state == {}


# --- answer builders ---------------------------------------------------------
from claude_rc.events import (  # noqa: E402
    cli_control_cancel_request,
    cli_dialog_response,
    cli_permission_response,
    cli_question_response,
)


def test_cli_user_message_pins_uuid():
    assert "uuid" not in cli_user_message("x")
    assert cli_user_message("x", message_uuid="u-1")["uuid"] == "u-1"


def test_cli_control_cancel_request_shape():
    assert cli_control_cancel_request("r9") == {"type": "control_cancel_request", "request_id": "r9"}


def test_cli_permission_response_shapes():
    allow = cli_permission_response("r1", allow=True, tool_input={"command": "ls"}, tool_use_id="toolu_1")
    assert allow == {"type": "control_response", "response": {
        "subtype": "success", "request_id": "r1", "tool_use_id": "toolu_1",
        "response": {"behavior": "allow", "updatedInput": {"command": "ls"}},
    }}
    # No input → no updatedInput at all (an empty one would blank the tool's input).
    assert cli_permission_response("r1", allow=True)["response"]["response"] == {"behavior": "allow"}
    deny = cli_permission_response("r2", allow=False)["response"]
    assert deny["response"] == {"behavior": "deny", "message": "Denied by controller"}
    assert "tool_use_id" not in deny


def test_cli_question_response_keeps_questions():
    questions = [{"question": "Pick one?", "header": "Pick", "options": [], "multiSelect": False}]
    r = cli_question_response("r3", {"Pick one?": "A"}, tool_input={"questions": questions}, tool_use_id="t")
    updated = r["response"]["response"]["updatedInput"]
    assert updated == {"questions": questions, "answers": {"Pick one?": "A"}}
    assert r["response"]["response"]["behavior"] == "allow"


def test_cli_dialog_response_shape():
    assert cli_dialog_response("r4", completed=False)["response"]["response"] == {"status": "cancelled", "result": {}}
    done = cli_dialog_response("r4", completed=True, result={"answers": ["x"]})
    assert done["response"]["response"] == {"status": "completed", "result": {"answers": ["x"]}}


# --- request/verdict plumbing (offline: transport stubbed) --------------------
def _ack(seq):
    return {"attestation_feedback": None, "results": [{"event_id": "e", "sequence_num": str(seq), "duplicate": False}]}


def _verdict_event(seq, request_id, **response):
    return Event.from_wire(_wire(
        {"type": "control_response", "response": {"request_id": request_id, **response}}, source="worker"
    ) | {"sequence_num": str(seq)})


def test_wait_control_response_scans_forward_from_the_request(monkeypatch):
    rc = _bare_rc()
    calls = []
    pages = {"40": [Event.from_wire(_wire({"type": "assistant"}) | {"sequence_num": "41"})],
             "41": [_verdict_event(42, "rid-1", subtype="success", response={"ok": 1})]}

    def fake_list(sid, **kw):
        calls.append(kw)
        return pages.get(kw.get("cursor"), [])

    monkeypatch.setattr(rc, "list_events", fake_list)
    monkeypatch.setattr("claude_rc.client.time.sleep", lambda s: None)
    verdict = rc.wait_control_response("cse_x", "rid-1", timeout=5, after_sequence_num=40)
    assert verdict == {"request_id": "rid-1", "subtype": "success", "response": {"ok": 1}}
    assert [c["cursor"] for c in calls] == ["40", "41"]  # the cursor advanced past seq 41
    assert all(c["sort_order"] == "asc" for c in calls)


def test_request_control_success_error_and_timeout(monkeypatch):
    from claude_rc.client import ControlRejected, ControlTimeout

    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or _ack(10))
    verdicts = iter([
        {"subtype": "success", "response": {"cancelled": True}},
        {"subtype": "error", "error": "stop_task: task_id must be a string"},
        None,
    ])
    seen_kw = []
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: seen_kw.append(kw) or next(verdicts))
    assert rc.cancel_queued_message("cse_x", "u-1") is True
    assert sent[0][0]["request"] == {"subtype": "cancel_async_message", "message_uuid": "u-1"}
    assert seen_kw[0]["after_sequence_num"] == 10
    with pytest.raises(ControlRejected, match="task_id"):
        rc.stop_task("cse_x", 123)
    with pytest.raises(ControlTimeout) as exc:
        rc.request_control("cse_x", "get_usage", timeout=0.1)
    assert exc.value.request_id.startswith("get_usage-")


def test_side_question_answer_and_cancel_on_timeout(monkeypatch):
    from claude_rc.client import ControlTimeout

    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or _ack(5))
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: {
        "subtype": "success", "request_id": rid, "response": {"response": "Writing tests.", "synthetic": False}})
    history = [{"question": "q0", "response": "a0", "extra": "dropped"}]
    assert rc.side_question("cse_x", "What now?", history=history) == "Writing tests."
    assert sent[0][0]["request"] == {
        "subtype": "side_question", "question": "What now?", "history": [{"question": "q0", "response": "a0"}],
    }

    sent.clear()
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: None)
    with pytest.raises(ControlTimeout):
        rc.side_question("cse_x", "Still there?", timeout=0.1)
    asked, cancelled = sent[0][0], sent[1][0]
    assert cancelled == {"type": "control_cancel_request", "request_id": asked["request_id"]}


def test_initialize_merges_pending_prompts(monkeypatch):
    rc = _bare_rc()
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: _ack(3))
    monkeypatch.setattr(rc, "wait_control_response", lambda sid, rid, **kw: {
        "subtype": "success", "response": {"claude_code_version": "2.1.301", "current_model": "m"},
        "pending_permission_requests": [{"request_id": "p"}], "pending_user_dialog_requests": [],
    })
    info = rc.initialize("cse_x")
    assert info["claude_code_version"] == "2.1.301"
    assert info["pending_permission_requests"] == [{"request_id": "p"}]


def test_session_reads_and_updates_are_unwrapped(monkeypatch):
    rc = _bare_rc()
    seen = []

    # `_request` takes a base-relative path and returns the parsed body.
    def fake_request(method, path, **kw):
        seen.append((method, path, kw.get("json")))
        return {"response_shape": {"id": "cse_x", "title": "a"}} if method == "GET" else {"session": {"id": "cse_x", "title": "b"}}

    monkeypatch.setattr(rc, "_request", fake_request)
    assert rc.get_session("cse_x") == {"id": "cse_x", "title": "a"}
    assert rc.rename_session("cse_x", "b") == {"id": "cse_x", "title": "b"}
    assert seen[1] == ("PUT", "/v1/code/sessions/cse_x", {"title": "b"})


def test_send_message_message_uuid(monkeypatch):
    rc = _bare_rc()
    sent = []
    monkeypatch.setattr(rc, "send_events", lambda sid, evs: sent.append(list(evs)) or {})
    rc.send_message("cse_x", "hello", message_uuid="u-7")
    assert sent[0][0]["uuid"] == "u-7"
