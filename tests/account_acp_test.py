"""ACP drives the existing fleet session without starting, resuming or replacing it."""

import asyncio
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from acp import RequestError
from acp.experimental.v2 import schema as s

bridge = runpy.run_path(str(Path(__file__).parents[1] / "scripts/cc-proxy-acp"), run_name="fleet_acp")
FleetAgent, HQ = bridge["FleetAgent"], bridge["HQ"]
SID = "fleet:opaque/worker?session=one"


def row(**changes):
    return {"id": SID, "host": "node-one", "workdir": "/remote/project", "title": "Existing work",
            "alive": True, "online": True, "lifecycle": "alive", "revision": "r1", "status": "idle",
            "transport": "native", "messages": [{"id": "before", "role": "assistant", "text": "Previous answer"}],
            **changes}


def mock_hq(*snapshots, listing=None):
    reads = iter(snapshots)
    actions = []

    async def detail(session_id):
        assert session_id == SID
        result = next(reads)
        if not bridge["available"](result):
            raise RequestError(-32000, "offline")
        return result

    async def action(session_id, action, **fields):
        actions.append((session_id, action, fields))
        return {"accepted": True}

    async def list_rows():
        return listing or [row()]

    return SimpleNamespace(detail=detail, action=action, list=list_rows, actions=actions)


def client():
    updates = []
    permissions = []

    async def session_update(session_id, update):
        updates.append((session_id, update.model_dump(by_alias=True, exclude_none=True)))

    async def request_permission(session_id, title, options, **kwargs):
        permissions.append((session_id, title, options))
        return s.RequestPermissionResponse(outcome=s.SelectedPermissionOutcomeVariant(option_id="deny"))

    return SimpleNamespace(session_update=session_update, updates=updates,
                           request_permission=request_permission, permissions=permissions)


async def no_wait(_):
    return None


def test_discovery_preserves_opaque_identity_and_skips_unreachable_sessions():
    async def check():
        hq = mock_hq(listing=[row(), row(id="ended", alive=False), row(id="offline", online=False)])
        agent = FleetAgent(hq, observe=False)
        response = await agent.list_sessions("/local/fleet")
        assert [entry.session_id for entry in response.sessions] == [SID]
        assert response.sessions[0].cwd == "/remote/project"
        assert hq.actions == []
    asyncio.run(check())


def test_provider_probe_and_history_load_never_write_to_original_process():
    async def check():
        hq = mock_hq(row())
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        probe = await agent.new_session("/local/fleet")
        with pytest.raises(RequestError, match="import an existing"):
            await agent.prompt(probe.session_id, [s.TextContentBlock(text="hello")])
        await agent.resume_session("/local/fleet", SID)
        assert conn.updates[0][1]["content"][0]["text"] == "Previous answer"
        assert hq.actions == []
        await agent.close_session(SID)
        assert hq.actions == []
    asyncio.run(check())


def test_prompt_waits_for_its_own_response_and_sends_once():
    async def check():
        before = row()["messages"]
        old_turn = row(messages=[*before, {"id": "unrelated", "role": "assistant", "text": "Older turn settled"}])
        complete = row(messages=[*old_turn["messages"], {"id": "user", "role": "user", "text": "new task"},
                                 {"id": "answer", "role": "assistant", "text": "Task complete"}])
        hq = mock_hq(row(), row(), old_turn, complete, complete)
        agent = FleetAgent(hq, sleep=no_wait, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.resume_session("/local/fleet", SID)
        result = await agent.prompt(SID, [s.TextContentBlock(text="new task")])
        assert result.message_id
        attached = agent.sessions[SID]
        await agent.poll(SID, attached)
        assert attached.pending == "new task"
        await agent.poll(SID, attached)
        await agent.poll(SID, attached)
        assert attached.pending is None
        assert hq.actions == [(SID, "send", {"text": "new task"})]
        assert [update["content"][0]["text"] for _, update in conn.updates if "content" in update] == [
            "Previous answer", "Older turn settled", "new task", "Task complete"]
        assert conn.updates[-1][1]["state"] == "idle"
    asyncio.run(check())


@pytest.mark.parametrize("content", [s.ImageContentBlock(type="image", data="AA==", mime_type="image/png"),
                                       s.TextContentBlock(text="x" * 16385),
                                       s.TextContentBlock(text="é" * 8193)])
def test_unsupported_prompt_is_rejected_before_any_write(content):
    async def check():
        hq = mock_hq(row())
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.resume_session("/local/fleet", SID)
        with pytest.raises(RequestError):
            await agent.prompt(SID, [content])
        assert hq.actions == []
    asyncio.run(check())


def test_uncertain_write_is_never_retried_or_replaced_with_a_new_session():
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=row())
        writes.append(request)
        raise httpx.ReadTimeout("connection lost after delivery", request=request)

    async def check():
        hq = HQ("http://hq.example", "toy-secret", transport=httpx.MockTransport(handle))
        with pytest.raises(RequestError, match="Delivery is unknown; do not resend"):
            await hq.action(SID, "send", text="hello")
        assert len(writes) == 1
        assert writes[0].url.raw_path.decode().startswith("/api/sessions/fleet%3Aopaque%2Fworker%3Fsession%3Done/action")
        await hq.http.aclose()
    asyncio.run(check())


def test_only_a_confirmed_stale_revision_can_retry():
    writes = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=row(revision=f"r{len(writes) + 1}"))
        writes.append(json.loads(request.content))
        return httpx.Response(409, text="refresh the session") if len(writes) == 1 else httpx.Response(200, json={"accepted": True})

    async def check():
        hq = HQ("http://hq.example", "toy-secret", transport=httpx.MockTransport(handle))
        await hq.action(SID, "send", text="hello")
        assert [body["revision"] for body in writes] == ["r1", "r2"]
        assert len({body["request_id"] for body in writes}) == 2
        with pytest.raises(RequestError, match="request changed"):
            await hq.action(SID, "respond", revision="r1", response={"behavior": "allow"})
        assert len(writes) == 2
        await hq.http.aclose()
    asyncio.run(check())


def test_native_permission_uses_original_request_and_cannot_grant_always():
    async def check():
        control = {"request_id": "native-request", "tool_name": "Bash", "input": {"command": "example"}}
        hq = mock_hq()
        agent = FleetAgent(hq)
        conn = client()
        agent.on_connect(conn)
        attached = bridge["Attached"]()
        await agent.handle_control(SID, row(native_control=control), attached)
        assert hq.actions == [(SID, "respond", {"revision": "r1", "response": {
            "request_id": "native-request", "behavior": "deny"}})]
        assert all(option.kind not in {"allow_always", "reject_always"} for option in conn.permissions[0][2])
        with pytest.raises(RequestError, match="native dialog"):
            await agent.handle_control(SID, row(native_control={**control, "request_id": "dialog",
                                       "subtype": "request_user_dialog"}), attached)
        assert len(hq.actions) == 1
    asyncio.run(check())


def test_explicit_cancel_interrupts_an_existing_bound_session_even_without_a_local_prompt():
    async def check():
        hq = mock_hq()
        agent = FleetAgent(hq)
        await agent.cancel_session(SID)
        assert hq.actions == []
        agent.sessions[SID] = bridge["Attached"]()
        await agent.cancel_session(SID)
        assert hq.actions == [(SID, "interrupt", {})]
    asyncio.run(check())


def test_native_questions_use_user_elicitation_not_permission_autoapproval():
    async def check():
        hq = mock_hq()
        agent = FleetAgent(hq)
        conn = client()

        async def create_elicitation(message, mode, **kwargs):
            assert kwargs["session_id"] == SID
            return s.AcceptElicitationResponse(content={"answer_0": "Option A"})

        conn.create_elicitation = create_elicitation
        agent.on_connect(conn)
        await agent.initialize(2, capabilities=s.ClientCapabilities.model_validate({"elicitation": {"form": {}}}))
        control = {"request_id": "question", "tool_name": "AskUserQuestion", "input": {"questions": [
            {"question": "Pick a value", "options": [{"label": "Option A"}, {"label": "Option B"}]}]}}
        await agent.handle_control(SID, row(native_control=control), bridge["Attached"]())
        assert hq.actions[0][2]["response"]["updated_input"]["answers"] == {"Pick a value": "Option A"}
        assert conn.permissions == []
    asyncio.run(check())


def test_growing_messages_keep_identity_and_replace_content_without_duplicate_text():
    async def check():
        first = row(messages=[{"role": "user", "text": "task"}, {"role": "assistant", "text": "Hello"}])
        second = row(messages=[{"role": "user", "text": "task"}, {"role": "assistant", "text": "Hello world"}])
        hq = mock_hq(first, second, second)
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.resume_session("/local/fleet", SID)
        await agent.poll(SID, agent.sessions[SID])
        await agent.poll(SID, agent.sessions[SID])
        updates = [update for _, update in conn.updates if update["sessionUpdate"] == "agent_message"]
        assert [update["content"][0]["text"] for update in updates] == ["Hello", "Hello world"]
        assert len({update["messageId"] for update in updates}) == 1
        assert hq.actions == []
    asyncio.run(check())


def test_native_message_growth_keeps_native_identity_across_reloads():
    async def check():
        final = row(messages=[*row()["messages"], {"id": "final", "role": "assistant", "text": "Hello world!"}])
        hq = mock_hq(row(), row(messages=[*row()["messages"], {"id": "final", "role": "assistant", "text": "Hello"}], status="working"),
                      final, final)
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.resume_session("/local/fleet", SID)
        for _ in range(2):
            await agent.poll(SID, agent.sessions[SID])
        await agent.resume_session("/local/fleet", SID)
        updates = [update for _, update in conn.updates if update["sessionUpdate"] == "agent_message"][1:]
        final_updates = [update for update in updates if update["messageId"] == "final"]
        assert [update["content"][0]["text"] for update in final_updates] == ["Hello", "Hello world!", "Hello world!"]
        assert [update["state"] for _, update in conn.updates if update["sessionUpdate"] == "state_update"] == ["running", "idle"]
    asyncio.run(check())


def test_real_sdk_stdio_negotiates_v2_and_provider_probe_creates_only_an_unbound_handle(tmp_path):
    async def check():
        token = tmp_path / "mcp-token"
        token.write_text("toy-token")
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).parents[1] / "scripts/cc-proxy-acp"),
            "--mcp-url", "https://mcp.example/mcp", "--mcp-token-file", str(token),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": 2, "info": {"name": "test", "version": "1"}}}).encode() + b"\n")
            await process.stdin.drain()
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
            assert response["result"]["protocolVersion"] == 2
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                "params": {"cwd": str(tmp_path)}}).encode() + b"\n")
            await process.stdin.drain()
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
            assert response["result"]["sessionId"].startswith("unbound:")
        finally:
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 10)
    asyncio.run(check())


def test_concurrent_prompts_deliver_only_one_action():
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        hq = mock_hq(row(), row(), row())
        original_action = hq.action

        async def action(*args, **kwargs):
            started.set()
            await release.wait()
            return await original_action(*args, **kwargs)

        hq.action = action
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.resume_session("/local/fleet", SID)
        first = asyncio.create_task(agent.prompt(SID, [s.TextContentBlock(text="first")]))
        await started.wait()
        second = asyncio.create_task(agent.prompt(SID, [s.TextContentBlock(text="second")]))
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[1], RequestError)
        assert hq.actions == [(SID, "send", {"text": "first"})]
    asyncio.run(check())


def test_uncertain_permission_delivery_is_not_offered_or_retried_again():
    async def check():
        calls = []
        hq = mock_hq()

        async def action(*args, **kwargs):
            calls.append((args, kwargs))
            raise RequestError(-32000, "Delivery is unknown; do not resend")

        hq.action = action
        agent = FleetAgent(hq)
        conn = client()
        agent.on_connect(conn)
        attached = bridge["Attached"]()
        snapshot = row(native_control={"request_id": "approval", "tool_name": "Bash", "input": {}})
        with pytest.raises(RequestError, match="Delivery is unknown"):
            await agent.handle_control(SID, snapshot, attached)
        await agent.handle_control(SID, snapshot, attached)
        assert len(calls) == len(conn.permissions) == 1
    asyncio.run(check())


def test_completed_legacy_messages_have_deterministic_identity_after_reload():
    history = [{"role": "user", "text": "repeat"}, {"role": "assistant", "text": "answer"},
               {"role": "user", "text": "repeat"}]
    first = bridge["identify_messages"](history, [])
    again = bridge["identify_messages"](history, [])
    assert [message["key"] for message in first] == [message["key"] for message in again]
    assert first[0]["key"] != first[2]["key"]
