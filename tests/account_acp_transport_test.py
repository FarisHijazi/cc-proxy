"""Real ACP stdio/MCP/HTTP fault-injection integration; no production agent or network."""

import asyncio
import json
import sys
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import pytest

SID = "fleet:isolated/worker?generation=one"
SCRIPT = Path(__file__).parents[1] / "scripts/cc-proxy-acp"


@pytest.fixture
def owner():
    lock = threading.Lock()
    state = {"row": {"id": SID, "alive": True, "online": True, "lifecycle": "alive", "revision": "r1",
                     "status": "idle", "host": "isolated-node", "workdir": "/isolated",
                     "messages": [{"id": "prior", "role": "assistant", "text": "Prior history"}]},
             "attempts": [], "delivered": [], "failure": None, "reads_after_send": 0,
             "control": None, "decisions": [], "http_reads": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            with lock:
                if unquote(self.path) != f"/api/sessions/{SID}":
                    self.reply(404, {})
                    return
                state["http_reads"] += 1
                if state["failure"] == "offline":
                    self.reply(503, {"error": "isolated outage"})
                    return
                if state["failure"] in {"ended", "restarted"}:
                    self.reply(404 if state["failure"] == "ended" else 409,
                               {"error": "The original worker identity is no longer live"})
                    return
                if state["delivered"]:
                    state["reads_after_send"] += 1
                    if state["reads_after_send"] == 2:
                        if state["control"]:
                            state["row"]["native_control"] = state["control"]
                        elif state["failure"] != "busy":
                            state["row"]["messages"].append({"id": "native-answer", "role": "assistant", "text": "ISOLATED_OK"})
                            state["row"]["status"] = "idle"
                self.reply(200, state["row"])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                if self.path == "/mcp":
                    assert self.headers.get("Authorization") == "Bearer isolated-token"
                    method = body["method"]
                    if "id" not in body:
                        self.reply(202, {})
                        return
                    if method == "initialize":
                        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "isolated-owner", "version": "1"}}
                    elif method == "tools/call":
                        assert body["params"]["name"] == "list_sessions"
                        result = {"content": [], "structuredContent": {"result": [state["row"]]}, "isError": False}
                    elif method == "tools/list":
                        result = {"tools": [{"name": "list_sessions", "inputSchema": {"type": "object"}}]}
                    else:
                        self.reply(400, {})
                        return
                    self.reply(200, {"jsonrpc": "2.0", "id": body["id"], "result": result})
                    return
                if unquote(self.path) != f"/api/sessions/{SID}/action":
                    self.reply(404, {})
                    return
                state["attempts"].append(body)
                if state["failure"] in {"stale", "rejected"}:
                    mode, state["failure"] = state["failure"], None
                    self.reply(409 if mode == "stale" else 422, {"error": "refresh" if mode == "stale" else "rejected"})
                    return
                if body["action"] == "send":
                    state["delivered"].append(body)
                    state["row"]["messages"].append({"id": "native-user", "role": "user", "text": body["text"]})
                    state["row"]["status"] = "running"
                    if state["failure"] == "lost_receipt":
                        self.reply(503, {"error": "isolated receipt loss after delivery"})
                        return
                elif body["action"] == "interrupt":
                    state["row"]["status"] = "idle"
                    state["row"].pop("native_control", None)
                    state["control"] = None
                elif body["action"] == "respond":
                    state["decisions"].append(body["response"])
                    state["row"].pop("native_control", None)
                    state["row"]["status"] = "idle"
                    state["row"]["messages"].append({"id": "native-answer", "role": "assistant", "text": "ISOLATED_OK"})
                self.reply(200, {"accepted": True, "request_id": body["request_id"]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@asynccontextmanager
async def connection(owner, tmp_path, *, permission="deny", form_action="accept", form_content=None,
                     cancel_on_request=False, discoverable=True):
    token = tmp_path / "isolated-token"
    token.write_text("isolated-token")
    process = await asyncio.create_subprocess_exec(sys.executable, str(SCRIPT),
        "--hq-url", owner["url"], "--mcp-url", owner["url"] + "/mcp", "--mcp-token-file", str(token),
        "--poll-interval", "0.02", "--turn-timeout", "3",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    updates, requests = [], []
    next_id = 0

    async def send(payload):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **payload}).encode() + b"\n")
        await process.stdin.drain()

    async def call(method, params, callback=None):
        nonlocal next_id
        next_id += 1
        request_id = next_id
        await send({"id": request_id, "method": method, "params": params})
        async with asyncio.timeout(10):
            while True:
                line = await process.stdout.readline()
                assert line, await process.stderr.read()
                packet = json.loads(line)
                if packet.get("method") == "session/update":
                    updates.append(packet["params"]["update"])
                    if callback:
                        await callback(packet["params"]["update"], send)
                elif packet.get("method") == "$/cancel_request":
                    updates.append({"sessionUpdate": "transport_cancel", "requestId": packet["params"]["requestId"]})
                elif "method" in packet and "id" in packet:
                    requests.append(packet)
                    if cancel_on_request:
                        await send({"method": "session/cancel", "params": {"sessionId": SID}})
                        continue
                    if packet["method"] == "session/request_permission":
                        result = {"outcome": {"outcome": "selected", "optionId": permission}}
                    elif packet["method"] == "elicitation/create":
                        result = {"action": form_action, "content":
                                  {"answer_0": "choice-one"} if form_content is None else
                                  form_content(len(requests)) if callable(form_content) else form_content}
                    else:
                        raise AssertionError(packet)
                    await send({"id": packet["id"], "result": result})
                elif packet.get("id") == request_id:
                    return packet

    try:
        initialized = await call("initialize", {"protocolVersion": 2, "clientCapabilities": {
            "_meta": {"t3.codes/externalSession": {"version": 1}}, "elicitation": {"form": {}}}})
        assert initialized["result"]["protocolVersion"] == 1
        listed = await call("session/list", {})
        assert "result" in listed, listed
        assert [item["sessionId"] for item in listed["result"]["sessions"]] == ([SID] if discoverable else [])
        loaded = await call("session/load", {"sessionId": SID, "cwd": "/isolated", "mcpServers": []})
        assert "result" in loaded, loaded
        yield call, updates, requests
    finally:
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0, await process.stderr.read()


def prompt_params():
    return {"sessionId": SID, "prompt": [{"type": "text", "text": "ISOLATED_REQUEST"}],
            "_meta": {"t3.codes/messageId": "app-user"}}


@pytest.mark.parametrize("fault", [None, "stale"])
def test_wire_prompt_waits_for_reply_and_reload_preserves_native_ids(owner, tmp_path, fault):
    async def check():
        owner["failure"] = fault
        async with connection(owner, tmp_path) as (call, updates, _):
            result = await call("session/prompt", prompt_params())
            assert result["result"]["stopReason"] == "end_turn", result
            assert [u["_meta"]["t3.codes/externalSessionDispatch"] for u in updates
                    if "t3.codes/externalSessionDispatch" in u.get("_meta", {})] == [
                        {"version": 1, "messageId": "app-user", "state": "accepted"}]
            assert [u["content"]["text"] for u in updates if u["sessionUpdate"] == "agent_message_chunk"] == ["Prior history", "ISOLATED_OK"]
            updates.clear()
            await call("session/load", {"sessionId": SID, "cwd": "/isolated", "mcpServers": []})
            assert [u["messageId"] for u in updates if "messageId" in u] == ["prior", "native-user", "native-answer"]
            assert len(owner["delivered"]) == 1
            assert len({a["request_id"] for a in owner["attempts"]}) == (2 if fault else 1)
        assert len(owner["delivered"]) == 1
    asyncio.run(check())


def test_wire_lost_receipt_blocks_second_write_even_when_owner_did_receive_it(owner, tmp_path):
    async def check():
        owner["failure"] = "lost_receipt"
        async with connection(owner, tmp_path) as (call, updates, __):
            first = await call("session/prompt", prompt_params())
            assert "Delivery is unknown" in first["error"]["message"]
            second = await call("session/prompt", prompt_params())
            assert "still being observed" in second["error"]["message"]
            assert len(owner["delivered"]) == len(owner["attempts"]) == 1
            assert not any("t3.codes/externalSessionDispatch" in u.get("_meta", {}) for u in updates)
    asyncio.run(check())


def test_wire_rejected_action_can_be_corrected_without_retrying_the_rejection(owner, tmp_path):
    async def check():
        owner["failure"] = "rejected"
        async with connection(owner, tmp_path) as (call, updates, __):
            first = await call("session/prompt", prompt_params())
            assert "rejected" in first["error"]["message"]
            assert len(owner["attempts"]) == 1 and owner["delivered"] == []
            assert not any("t3.codes/externalSessionDispatch" in u.get("_meta", {}) for u in updates)
            second = await call("session/prompt", prompt_params())
            assert second["result"]["stopReason"] == "end_turn"
            assert len(owner["attempts"]) == 2 and len(owner["delivered"]) == 1
    asyncio.run(check())


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_wire_permission_posts_only_the_explicit_owner_decision(owner, tmp_path, decision):
    async def check():
        owner["control"] = {"request_id": "native-control", "tool_name": "Bash", "input": {"command": "true"}}
        async with connection(owner, tmp_path, permission=decision) as (call, _, requests):
            result = await call("session/prompt", prompt_params())
            assert result["result"]["stopReason"] == "end_turn", result
            assert len(requests) == len(owner["decisions"]) == 1
            assert owner["decisions"][0]["behavior"] == decision
            assert owner["decisions"][0]["request_id"] == "native-control"
    asyncio.run(check())


def test_wire_cancel_waits_for_owner_idle_and_preserves_history(owner, tmp_path):
    async def check():
        owner["failure"] = "busy"
        sent = False
        async def stop_running(update, send):
            nonlocal sent
            state = update.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state")
            if state == "running" and not sent:
                sent = True
                await send({"method": "session/cancel", "params": {"sessionId": SID}})
        async with connection(owner, tmp_path) as (call, updates, _):
            result = await call("session/prompt", prompt_params(), stop_running)
            assert result["result"]["stopReason"] == "cancelled", result
            assert [a["action"] for a in owner["attempts"]] == ["send", "interrupt"]
            states = [u["_meta"]["t3.codes/externalSessionState"]["state"] for u in updates if "t3.codes/externalSessionState" in u.get("_meta", {})]
            assert states[-1] == "idle"
            assert [m["id"] for m in owner["row"]["messages"]] == ["prior", "native-user"]
    asyncio.run(check())


@pytest.mark.parametrize("choice", ["accept", "decline", "cancel"])
def test_wire_question_accept_decline_and_cancel_never_autoapprove(owner, tmp_path, choice):
    async def check():
        owner["control"] = {"request_id": "question-one", "tool_name": "AskUserQuestion", "input": {
            "questions": [{"question": "Choose a value", "options": [{"label": "choice-one"}, {"label": "choice-two"}]}]}}
        async with connection(owner, tmp_path, form_action=choice) as (call, _, requests):
            result = await call("session/prompt", prompt_params())
            assert len(requests) == 1, result
            assert requests[0]["method"] == "elicitation/create"
            assert requests[0]["params"]["sessionId"] == SID
            if choice == "accept":
                assert result["result"]["stopReason"] == "end_turn", result
                assert owner["decisions"][0]["updated_input"]["answers"] == {"Choose a value": "choice-one"}
            else:
                assert "timed out" in result["error"]["message"], result
                assert owner["decisions"] == []
                assert owner["row"]["native_control"]["request_id"] == "question-one"
    asyncio.run(check())


def test_wire_offline_observation_fails_closed_and_reconnect_reloads_same_source(owner, tmp_path):
    async def check():
        owner["failure"] = "busy"
        async def disconnect_running(update, send):
            state = update.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state")
            if state == "running":
                owner["failure"] = "offline"
        async with connection(owner, tmp_path) as (call, updates, _):
            result = await call("session/prompt", prompt_params(), disconnect_running)
            assert "observation failed" in result["error"]["message"]
            assert len(owner["attempts"]) == 1
            assert any(u.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state") == "unavailable" for u in updates)
        owner["failure"] = "busy"
        async with connection(owner, tmp_path) as (_, updates, __):
            assert [u["messageId"] for u in updates if "messageId" in u] == ["prior", "native-user"]
            assert len(owner["attempts"]) == 1
    asyncio.run(check())


@pytest.mark.parametrize("lifecycle", ["ended", "restarted"])
def test_wire_original_worker_loss_does_not_retarget_or_retry(owner, tmp_path, lifecycle):
    async def check():
        owner["failure"] = "busy"
        async def lose_original_worker(update, send):
            state = update.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state")
            if state == "running":
                owner["failure"] = lifecycle
        async with connection(owner, tmp_path) as (call, updates, _):
            result = await call("session/prompt", prompt_params(), lose_original_worker)
            assert "observation failed" in result["error"]["message"]
            second = await call("session/prompt", prompt_params())
            assert "still being observed" in second["error"]["message"]
            reloaded = await call("session/load", {"sessionId": SID, "cwd": "/isolated", "mcpServers": []})
            assert "Fleet HQ read failed" in reloaded["error"]["message"]
            assert len(owner["attempts"]) == len(owner["delivered"]) == 1
            assert any(u.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state") == "unavailable" for u in updates)
    asyncio.run(check())


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_wire_permission_already_pending_before_attach_needs_no_local_prompt(owner, tmp_path, decision):
    async def check():
        owner["row"]["native_control"] = {"request_id": "existing-permission", "tool_name": "Bash",
                                            "input": {"command": "true"}}
        async with connection(owner, tmp_path, permission=decision) as (call, _, requests):
            async with asyncio.timeout(10):
                while not owner["decisions"]:
                    await call("session/list", {})
            assert len(requests) == 1
            assert owner["decisions"][0] == {"request_id": "existing-permission", "behavior": decision,
                **({"updated_input": {"command": "true"}} if decision == "allow" else {})}
            assert [a["action"] for a in owner["attempts"]] == ["respond"]
            assert owner["delivered"] == []
    asyncio.run(check())


@pytest.mark.parametrize("decision", ["accept", "decline", "cancel"])
def test_wire_question_already_pending_before_attach_preserves_owner_choice(owner, tmp_path, decision):
    async def check():
        owner["row"]["native_control"] = {"request_id": "existing-question", "tool_name": "AskUserQuestion",
            "input": {"questions": [{"question": "Which?", "options": [{"label": "choice-one"}]}]}}
        async with connection(owner, tmp_path, form_action=decision) as (call, _, requests):
            async with asyncio.timeout(10):
                while not requests:
                    await call("session/list", {})
                # Let the response travel back to the owner, then observe again.
                for _ in range(3):
                    await call("session/list", {})
            assert len(requests) == 1
            assert owner["delivered"] == []
            if decision == "accept":
                assert owner["decisions"][0]["updated_input"]["answers"] == {"Which?": "choice-one"}
                assert [a["action"] for a in owner["attempts"]] == ["respond"]
            else:
                assert owner["attempts"] == []
                assert owner["row"]["native_control"]["request_id"] == "existing-question"
    asyncio.run(check())


def test_wire_stop_with_open_permission_cancels_exact_request_after_owner_idle(owner, tmp_path):
    async def check():
        owner["control"] = {"request_id": "open-permission", "tool_name": "Bash", "input": {"command": "true"}}
        async with connection(owner, tmp_path, cancel_on_request=True) as (call, updates, requests):
            result = await call("session/prompt", prompt_params())
            assert result["result"]["stopReason"] == "cancelled", result
            while not any(u["sessionUpdate"] == "transport_cancel" for u in updates):
                await call("session/list", {})
            assert [a["action"] for a in owner["attempts"]] == ["send", "interrupt"]
            assert owner["decisions"] == []
            assert [u["requestId"] for u in updates if u["sessionUpdate"] == "transport_cancel"] == [requests[0]["id"]]
    asyncio.run(check())


def test_wire_stop_with_question_pending_at_attach_needs_no_prompt_or_answer(owner, tmp_path):
    async def check():
        owner["row"]["native_control"] = {"request_id": "existing-question", "tool_name": "AskUserQuestion",
            "input": {"questions": [{"question": "Which?", "options": [{"label": "choice-one"}]}]}}
        async with connection(owner, tmp_path, cancel_on_request=True) as (call, updates, requests):
            async with asyncio.timeout(8):
                while not any(u["sessionUpdate"] == "transport_cancel" for u in updates):
                    await call("session/list", {})
            assert [a["action"] for a in owner["attempts"]] == ["interrupt"]
            assert owner["decisions"] == owner["delivered"] == []
            assert [u["requestId"] for u in updates if u["sessionUpdate"] == "transport_cancel"] == [requests[0]["id"]]
    asyncio.run(check())


@pytest.mark.parametrize('multi,content,expected', [
    (False, {'answer_0__other': 'custom-wire-answer'}, 'custom-wire-answer'),
    (True, {'answer_0': ['choice-one', 'choice-two']}, 'choice-one, choice-two'),
    (True, {'answer_0': ['__cc_fleet_other__'], 'answer_0__other': 'custom-only'}, 'custom-only'),
])
def test_wire_native_question_preserves_other_and_multiple_selected_values(owner, tmp_path, multi, content, expected):
    async def check():
        owner['control'] = {'request_id': 'choices', 'tool_name': 'AskUserQuestion', 'input': {'questions': [
            {'id': 'native-q', 'question': 'Which?', 'multiSelect': multi,
             'options': [{'label': 'choice-one'}, {'label': 'choice-two'}]}]}}
        async with connection(owner, tmp_path, form_content=content) as (call, _, requests):
            result = await call('session/prompt', prompt_params())
            assert result['result']['stopReason'] == 'end_turn', result
            assert len(requests) == 1
            assert owner['decisions'][0]['updated_input']['answers'] == {'native-q': expected}
    asyncio.run(check())


def test_wire_invalid_answer_reopens_current_question_without_posting_until_corrected(owner, tmp_path):
    async def check():
        owner['control'] = {'request_id': 'correctable', 'tool_name': 'AskUserQuestion', 'input': {'questions': [
            {'question': 'Which?', 'options': [{'label': 'choice-one'}]}]}}
        def answer(number):
            assert owner['decisions'] == []
            return {} if number == 1 else {'answer_0': 'choice-one'}
        async with connection(owner, tmp_path, form_content=answer) as (call, updates, requests):
            result = await call('session/prompt', prompt_params())
            assert result['result']['stopReason'] == 'end_turn', result
            assert len(requests) == 2 and requests[0]['id'] != requests[1]['id']
            assert [action['action'] for action in owner['attempts']] == ['send', 'respond']
            assert owner['decisions'][0]['updated_input']['answers'] == {'Which?': 'choice-one'}
            assert any(u.get('title') == 'Answer needs correction' for u in updates)
    asyncio.run(check())


def test_wire_archived_history_loads_without_rediscovery_or_control(owner, tmp_path):
    async def check():
        owner["row"].update(provider="codex", transport="codex", alive=False, online=True, lifecycle="ended", read_only=True,
                            status="ended", capabilities={"send": False, "interrupt": False})
        async with connection(owner, tmp_path, discoverable=False) as (call, updates, requests):
            assert [(u["messageId"], u["content"]["text"]) for u in updates
                    if u["sessionUpdate"] == "agent_message_chunk"] == [("prior", "Prior history")]
            assert any(u.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state")
                       == "unavailable" for u in updates)
            result = await call("session/prompt", prompt_params())
            assert "offline or ended" in result["error"]["message"]
            assert owner["attempts"] == owner["delivered"] == requests == []
            updates.clear()
            result = await call("session/load", {"sessionId": SID, "cwd": "/isolated", "mcpServers": []})
            assert "result" in result
            assert [u["messageId"] for u in updates if "messageId" in u] == ["prior"]
            assert owner["attempts"] == []
    asyncio.run(check())


def test_wire_session_becoming_read_only_stops_observation_without_another_action(owner, tmp_path):
    async def check():
        owner["failure"] = "busy"

        async def archive_running_session(update, send):
            if update.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state") == "running":
                owner["row"].update(provider="codex", transport="codex", alive=False, online=True, lifecycle="ended", read_only=True,
                                    status="ended", capabilities={"send": False, "interrupt": False})

        async with connection(owner, tmp_path) as (call, updates, requests):
            result = await call("session/prompt", prompt_params(), archive_running_session)
            assert "observation failed" in result["error"]["message"]
            assert any(u.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state")
                       == "unavailable" for u in updates)
            assert [action["action"] for action in owner["attempts"]] == ["send"]
            assert requests == []
            updates.clear()
            result = await call("session/load", {"sessionId": SID, "cwd": "/isolated", "mcpServers": []})
            assert "result" in result
            assert [u["messageId"] for u in updates if "messageId" in u] == ["prior", "native-user"]
            assert len(owner["attempts"]) == 1
    asyncio.run(check())


@pytest.mark.parametrize(('provider', 'transport', 'engine'), [
    ('claude', 'native', 'claude-code'), ('codex', 'codex', 'codex'),
])
def test_wire_identity_uses_owner_engine_and_preserves_attachment_history(owner, tmp_path, provider, transport, engine):
    async def check():
        owner['row'].update(provider=provider, transport=transport, model='owner-model',
                            capabilities={'send': True, 'interrupt': True, 'set_model': True},
                            title='Other agent name is not identity')
        async with connection(owner, tmp_path) as (call, updates, requests):
            listed = await call('session/list', {})
            entry = listed['result']['sessions'][0]
            assert entry['sessionId'] == SID
            identity = entry['_meta']['t3.codes/externalSessionIdentity']
            assert identity['engine'] == engine
            assert identity['connection'] == {'kind': 'fleet', 'host': 'isolated-node', 'transport': transport}
            assert identity['model'] == 'owner-model'
            assert identity['capabilities']['send'] is True
            assert identity['capabilities']['setModel'] is False
            assert [(u['messageId'], u['content']['text']) for u in updates
                    if u['sessionUpdate'] == 'agent_message_chunk'] == [('prior', 'Prior history')]
            owner['row']['model'] = 'owner-model-changed'
            loaded = await call('session/load', {'sessionId': SID, 'cwd': '/isolated', 'mcpServers': []})
            updated = loaded['result']['_meta']['t3.codes/externalSessionIdentity']
            assert updated['engine'] == engine
            assert updated['model'] == 'owner-model-changed'
            assert any(u.get('_meta', {}).get('t3.codes/externalSessionIdentity') == updated for u in updates)
            assert owner['attempts'] == owner['delivered'] == requests == []
    asyncio.run(check())
