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
from acp import schema as s

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
    sent = asyncio.Event()

    async def detail(session_id):
        assert session_id == SID
        result = next(reads)
        if not bridge["available"](result):
            raise RequestError(-32000, "offline")
        return result

    async def action(session_id, action, **fields):
        actions.append((session_id, action, fields))
        sent.set()
        return {"accepted": True}

    async def list_rows():
        return listing or [row()]

    return SimpleNamespace(detail=detail, action=action, list=list_rows, actions=actions, sent=sent)


def client():
    updates = []
    permissions = []

    async def session_update(session_id, update):
        updates.append((session_id, update.model_dump(by_alias=True, exclude_none=True)))

    async def request_permission(session_id, title, options, **kwargs):
        permissions.append((session_id, title, options))
        return s.RequestPermissionResponse.model_validate({"outcome": {"outcome": "selected", "optionId": "deny"}})

    return SimpleNamespace(session_update=session_update, updates=updates,
                           request_permission=request_permission, permissions=permissions)


async def no_wait(_):
    return None


async def begin_prompt(agent, hq, text):
    hq.sent.clear()
    task = asyncio.create_task(agent.prompt(SID, [s.TextContentBlock(text=text)]))
    await hq.sent.wait()
    return task


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
        assert conn.updates[0][1]["content"]["text"] == "Previous answer"
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
        prompt = await begin_prompt(agent, hq, "new task")
        assert not prompt.done()
        attached = agent.sessions[SID]
        await agent.poll(SID, attached)
        assert attached.pending == "new task"
        await agent.poll(SID, attached)
        await agent.poll(SID, attached)
        assert attached.pending is None
        assert hq.actions == [(SID, "send", {"text": "new task"})]
        assert [update["content"]["text"] for _, update in conn.updates if "content" in update] == [
            "Previous answer", "Older turn settled", "Task complete"]
        assert (await prompt).stop_reason == "end_turn"
    asyncio.run(check())


def test_prompt_echo_is_not_duplicated_and_load_preserves_native_identity():
    async def check():
        complete = row(messages=[*row()["messages"],
            {"id": "native-user", "role": "user", "text": "new task"},
            {"id": "native-answer", "role": "assistant", "text": "Task complete"}])
        hq = mock_hq(row(), row(), complete, complete, complete)
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.load_session("/local/fleet", SID)
        prompt = await begin_prompt(agent, hq, "new task")
        await agent.poll(SID, agent.sessions[SID])
        await agent.poll(SID, agent.sessions[SID])
        assert (await prompt).stop_reason == "end_turn"
        assert not any(update["sessionUpdate"] == "user_message_chunk" for _, update in conn.updates)
        conn.updates.clear()
        await agent.load_session("/local/fleet", SID)
        replayed = [update for _, update in conn.updates]
        assert [update["messageId"] for update in replayed] == ["before", "native-user", "native-answer"]
        assert [update["content"]["text"] for update in replayed] == ["Previous answer", "new task", "Task complete"]
        assert hq.actions == [(SID, "send", {"text": "new task"})]
    asyncio.run(check())


def test_uncertain_cancel_is_visible_and_never_marks_the_worker_stopped():
    async def check():
        hq = mock_hq(row(status="working"))
        attempts = []
        async def uncertain(session_id, action, **fields):
            attempts.append((session_id, action))
            raise RequestError(-32000, "Delivery is unknown; do not resend")
        hq.action = uncertain
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.resume_session("/local/fleet", SID)
        await agent.cancel(SID)
        assert attempts == [(SID, "interrupt")]
        assert agent.sessions[SID].cancelled is False
        assert conn.updates[-1][1]["sessionUpdate"] == "notice"
        assert conn.updates[-1][1]["severity"] == "error"
        assert "No action was retried" in conn.updates[-1][1]["description"]
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
        await agent.cancel(SID)
        assert hq.actions == []
        agent.sessions[SID] = bridge["Attached"]()
        await agent.cancel(SID)
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
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({"elicitation": {"form": {}}}))
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
        updates = [update for _, update in conn.updates if update["sessionUpdate"] == "agent_message_chunk"]
        assert [update["content"]["text"] for update in updates] == ["Hello", " world"]
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
        updates = [update for _, update in conn.updates if update["sessionUpdate"] == "agent_message_chunk"][1:]
        final_updates = [update for update in updates if update["messageId"] == "final"]
        assert [update["content"]["text"] for update in final_updates] == ["Hello", " world!", "Hello world!"]
        assert not any(update["sessionUpdate"] == "state_update" for _, update in conn.updates)
    asyncio.run(check())


@pytest.mark.parametrize("requested_version", [1, 2])
def test_real_sdk_stdio_negotiates_v1_and_provider_probe_creates_only_an_unbound_handle(tmp_path, requested_version):
    async def check():
        token = tmp_path / "mcp-token"
        token.write_text("toy-token")
        process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).parents[1] / "scripts/cc-proxy-acp"),
            "--mcp-url", "https://mcp.example/mcp", "--mcp-token-file", str(token),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": requested_version, "clientInfo": {"name": "test", "version": "1"}}}).encode() + b"\n")
            await process.stdin.drain()
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
            assert response["result"]["protocolVersion"] == 1
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new",
                "params": {"cwd": str(tmp_path), "mcpServers": []}}).encode() + b"\n")
            await process.stdin.drain()
            response = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
            assert "result" in response, response
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
        with pytest.raises(RequestError, match="still being observed"):
            await second
        agent.finish(agent.sessions[SID], "end_turn")
        assert (await first).stop_reason == "end_turn"
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


def test_prompt_reply_stays_pending_until_new_assistant_and_confirmed_idle():
    async def check():
        sent = asyncio.Event()
        before = row()["messages"]
        answer = [*before, {"id": "user", "role": "user", "text": "new task"},
                  {"id": "answer", "role": "assistant", "text": "Task complete"}]
        hq = mock_hq(row(), row(), row(messages=answer, status="working"),
                      row(messages=answer), row(messages=answer))
        original = hq.action
        async def action(*args, **kwargs):
            result = await original(*args, **kwargs)
            sent.set()
            return result
        hq.action = action
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.resume_session("/local/fleet", SID)
        prompt = asyncio.create_task(agent.prompt(SID, [s.TextContentBlock(text="new task")]))
        await sent.wait()
        try:
            assert not prompt.done(), "An insertion acknowledgement is not the completed reply"
            for _ in range(2):
                await agent.poll(SID, agent.sessions[SID])
                assert not prompt.done()
            await agent.poll(SID, agent.sessions[SID])
            result = await prompt
            assert result.stop_reason == "end_turn"
            assert hq.actions == [(SID, "send", {"text": "new task"})]
        finally:
            prompt.cancel()
    asyncio.run(check())


def test_cancel_waits_for_observed_idle_and_does_not_close_the_worker():
    async def check():
        hq = mock_hq(row(), row(), row(status="working"), row())
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        prompt = await begin_prompt(agent, hq, "long task")
        await agent.cancel(SID)
        await agent.poll(SID, agent.sessions[SID])
        assert not prompt.done()
        await agent.poll(SID, agent.sessions[SID])
        assert (await prompt).stop_reason == "cancelled"
        await agent.close_session(SID)
        assert hq.actions == [(SID, "send", {"text": "long task"}), (SID, "interrupt", {})]
    asyncio.run(check())


def test_idle_reads_cannot_complete_a_prompt_before_its_reply():
    async def check():
        answer = row(messages=[*row()["messages"], {"id": "user", "role": "user", "text": "task"},
                               {"id": "reply", "role": "assistant", "text": "Done"}])
        hq = mock_hq(row(), row(), row(), row(), answer, answer)
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        await agent.poll(SID, agent.sessions[SID])
        prompt = await begin_prompt(agent, hq, "task")
        await agent.poll(SID, agent.sessions[SID])
        assert not prompt.done()
        await agent.poll(SID, agent.sessions[SID])
        assert not prompt.done()
        await agent.poll(SID, agent.sessions[SID])
        assert (await prompt).stop_reason == "end_turn"
    asyncio.run(check())


def test_observer_failure_fails_pending_prompt_without_resending():
    async def check():
        hq = mock_hq(row(), row())
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        prompt = await begin_prompt(agent, hq, "task")
        async def unavailable(_):
            raise ValueError("malformed remote response")
        hq.detail = unavailable
        advance = asyncio.Event()
        async def step(_):
            await advance.wait()
            advance.clear()
        agent.sleep = step
        attached = agent.sessions[SID]
        attached.observer = asyncio.create_task(agent.watch(SID, attached))
        advance.set()
        with pytest.raises(RequestError, match="observation failed"):
            await prompt
        with pytest.raises(RequestError, match="still being observed"):
            await agent.prompt(SID, [s.TextContentBlock(text="task")])
        await agent.close_session(SID)
        assert hq.actions == [(SID, "send", {"text": "task"})]
    asyncio.run(check())


def test_prompt_deadline_applies_even_when_observer_cannot_make_progress():
    async def check():
        hq = mock_hq(row(), row())
        agent = FleetAgent(hq, observe=False, turn_timeout=0.01)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        with pytest.raises(RequestError, match="Observation timed out"):
            await agent.prompt(SID, [s.TextContentBlock(text="task")])
        with pytest.raises(RequestError, match="still being observed"):
            await agent.prompt(SID, [s.TextContentBlock(text="task")])
        assert hq.actions == [(SID, "send", {"text": "task"})]
    asyncio.run(check())


@pytest.mark.parametrize("status", [None, "unknown", "waiting"])
def test_cancel_requires_positive_idle_evidence(status):
    async def check():
        hq = mock_hq(row(), row(), row(status=status), row())
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        prompt = await begin_prompt(agent, hq, "task")
        await agent.cancel(SID)
        await agent.poll(SID, agent.sessions[SID])
        assert not prompt.done()
        await agent.poll(SID, agent.sessions[SID])
        assert (await prompt).stop_reason == "cancelled"
    asyncio.run(check())


def test_older_assistant_growth_cannot_be_the_new_prompt_reply():
    async def check():
        echo = row(messages=[{"id": "before", "role": "assistant", "text": "Previous answer continues"},
                             {"id": "user", "role": "user", "text": "task"}])
        older = row(messages=[{"id": "before", "role": "assistant", "text": "Previous answer continues again"},
                              {"id": "user", "role": "user", "text": "task"}])
        answered = row(messages=[*older["messages"], {"id": "new", "role": "assistant", "text": "New answer"}])
        hq = mock_hq(row(), row(), echo, older, older, answered, answered)
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        prompt = await begin_prompt(agent, hq, "task")
        for _ in range(3):
            await agent.poll(SID, agent.sessions[SID])
            assert not agent.sessions[SID].answered
            assert not prompt.done()
        await agent.poll(SID, agent.sessions[SID])
        await agent.poll(SID, agent.sessions[SID])
        assert (await prompt).stop_reason == "end_turn"
    asyncio.run(check())


def test_concurrent_and_unknown_cancel_are_never_retried():
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        hq = mock_hq(row())
        async def action(session_id, action, **fields):
            calls.append((session_id, action))
            started.set()
            await release.wait()
            raise RequestError(-32000, "Delivery is unknown")
        hq.action = action
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        first = asyncio.create_task(agent.cancel(SID))
        await started.wait()
        await agent.cancel(SID)
        release.set()
        await first
        await agent.cancel(SID)
        assert calls == [(SID, "interrupt")]
        assert not agent.sessions[SID].cancelled
    asyncio.run(check())


def test_cancel_cannot_confirm_using_a_read_started_before_its_action():
    async def check():
        started, release = asyncio.Event(), asyncio.Event()
        hq = mock_hq(row(), row(), row())
        original = hq.detail
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        prompt = await begin_prompt(agent, hq, "task")
        async def delayed(session_id):
            started.set()
            await release.wait()
            return row()
        hq.detail = delayed
        poll = asyncio.create_task(agent.poll(SID, agent.sessions[SID]))
        await started.wait()
        cancel = asyncio.create_task(agent.cancel(SID))
        release.set()
        await poll
        await cancel
        assert not prompt.done()
        hq.detail = original
        await agent.poll(SID, agent.sessions[SID])
        assert (await prompt).stop_reason == "cancelled"
    asyncio.run(check())


def test_cancel_queued_during_pre_send_read_is_still_sent_only_once():
    async def check():
        reading, release, interrupting, finish_interrupt = (asyncio.Event() for _ in range(4))
        hq = mock_hq(row(), row())
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        async def delayed_read(_):
            reading.set()
            await release.wait()
            return row()
        original_action = hq.action
        async def action(session_id, action, **fields):
            result = await original_action(session_id, action, **fields)
            if action == "interrupt":
                interrupting.set()
                await finish_interrupt.wait()
            return result
        hq.detail, hq.action = delayed_read, action
        prompt = asyncio.create_task(agent.prompt(SID, [s.TextContentBlock(text="task")]))
        await reading.wait()
        first = asyncio.create_task(agent.cancel(SID))
        release.set()
        await interrupting.wait()
        await agent.cancel(SID)
        finish_interrupt.set()
        await first
        await agent.poll(SID, agent.sessions[SID])
        assert (await prompt).stop_reason == "cancelled"
        assert hq.actions == [(SID, "send", {"text": "task"}), (SID, "interrupt", {})]
    asyncio.run(check())


def test_definitively_rejected_prompt_does_not_block_another_send():
    async def check():
        writes, sent = [], asyncio.Event()
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json=row())
            writes.append(json.loads(request.content))
            if len(writes) == 1:
                return httpx.Response(403, json={"error": "action refused"})
            sent.set()
            return httpx.Response(200, json={"accepted": True})
        hq = HQ("http://hq.example", "toy-secret", transport=httpx.MockTransport(handle))
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        with pytest.raises(RequestError, match="rejected"):
            await agent.prompt(SID, [s.TextContentBlock(text="first")])
        assert agent.sessions[SID].pending is None
        second = asyncio.create_task(agent.prompt(SID, [s.TextContentBlock(text="second")]))
        await asyncio.wait_for(sent.wait(), 1)
        assert [write["text"] for write in writes] == ["first", "second"]
        second.cancel()
        await agent.close_session(SID)
        await hq.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["native", "menu"])
def test_stale_control_is_offered_again_with_a_fresh_revision(kind):
    async def check():
        writes = []
        control = {"native_control": {"request_id": "permission", "tool_name": "Bash", "input": {}}} if kind == "native" else {
            "menu": {"signature": "menu-one", "question": "Pick", "options": [{"index": 0, "label": "A"}]}}
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json=row(revision="r2", **control))
            writes.append(json.loads(request.content))
            return httpx.Response(200, json={"accepted": True})
        hq = HQ("http://hq.example", "toy-secret", transport=httpx.MockTransport(handle))
        agent = FleetAgent(hq, observe=False)
        conn, forms = client(), []
        async def answer(message, mode, **kwargs):
            forms.append(message)
            return s.AcceptElicitationResponse(content={"choice": "0"})
        conn.create_elicitation = answer
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({"elicitation": {"form": {}}}))
        attached = bridge["Attached"]()
        await agent.handle_control(SID, row(revision="r1", **control), attached)
        assert not attached.controls
        await agent.handle_control(SID, row(revision="r2", **control), attached)
        assert len(conn.permissions if kind == "native" else forms) == 2
        assert len(writes) == 1 and writes[0]["revision"] == "r2"
        await hq.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("kind", ["native", "menu"])
def test_declined_control_stays_suppressed_until_its_identity_changes(kind):
    async def check():
        hq = mock_hq()
        agent = FleetAgent(hq, observe=False)
        conn, forms = client(), []
        async def decline(message, mode, **kwargs):
            forms.append(message)
            return s.DeclineElicitationResponse()
        conn.create_elicitation = decline
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({"elicitation": {"form": {}}}))
        def question(key):
            return row(native_control={"request_id": key, "tool_name": "AskUserQuestion", "input": {
                "questions": [{"question": "Pick", "options": [{"label": "A"}]}]}}) if kind == "native" else row(
                    menu={"signature": key, "question": "Pick", "options": [{"index": 0, "label": "A"}]})
        attached = bridge["Attached"]()
        await agent.handle_control(SID, question("one"), attached)
        await agent.handle_control(SID, question("one"), attached)
        assert len(forms) == 1 and hq.actions == []
        await agent.handle_control(SID, question("two"), attached)
        assert len(forms) == 2 and hq.actions == []
    asyncio.run(check())


@pytest.mark.parametrize("failure_mode", ["transport", "server_error", "bad_receipt", "in_progress_receipt", "conflicting_receipt"])
def test_ambiguous_send_keeps_the_pending_fence(failure_mode):
    async def check():
        writes = []
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json=row())
            writes.append(request)
            if failure_mode == "transport":
                raise httpx.ReadTimeout("response lost", request=request)
            if failure_mode == "server_error":
                return httpx.Response(502, text="upstream unavailable")
            if failure_mode == "in_progress_receipt":
                return httpx.Response(409, text="Request is already in progress; do not resend with a new ID")
            if failure_mode == "conflicting_receipt":
                return httpx.Response(409, text="request_id was already used with a different payload")
            return httpx.Response(200, text="invalid receipt")
        hq = HQ("http://hq.example", "toy-secret", transport=httpx.MockTransport(handle))
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.load_session("/local/fleet", SID)
        with pytest.raises(RequestError, match="Delivery is unknown"):
            await agent.prompt(SID, [s.TextContentBlock(text="task")])
        with pytest.raises(RequestError, match="still being observed"):
            await agent.prompt(SID, [s.TextContentBlock(text="task")])
        assert len(writes) == 1
        await agent.close_session(SID)
        await hq.http.aclose()
    asyncio.run(check())


def test_owner_rejected_control_requires_fresh_consent_before_another_post():
    async def check():
        writes = []
        snapshot = row(native_control={"request_id": "permission", "tool_name": "Bash", "input": {}})
        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json=snapshot)
            writes.append(json.loads(request.content))
            return httpx.Response(409, text="refresh the session") if len(writes) == 1 else httpx.Response(200, json={"accepted": True})
        hq = HQ("http://hq.example", "toy-secret", transport=httpx.MockTransport(handle))
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        attached = bridge["Attached"]()
        await agent.handle_control(SID, snapshot, attached)
        assert len(writes) == 1 and len(conn.permissions) == 1
        assert not attached.controls
        await agent.handle_control(SID, snapshot, attached)
        assert len(writes) == 2 and len(conn.permissions) == 2
        assert writes[0]["request_id"] != writes[1]["request_id"]
        await hq.http.aclose()
    asyncio.run(check())


def test_selected_external_session_control_requires_the_owners_explicit_decision():
    async def check():
        offered, decision = asyncio.Event(), asyncio.Event()
        snapshot = row(native_control={"request_id": "external-permission", "tool_name": "Bash", "input": {"command": "example"}})
        hq = mock_hq(snapshot, snapshot)
        agent = FleetAgent(hq, observe=False)
        conn = client()
        async def approve(session_id, tool_call, options, **kwargs):
            assert session_id == SID and tool_call.tool_call_id == "external-permission"
            assert [option.kind for option in options] == ["allow_once", "reject_once"]
            offered.set()
            await decision.wait()
            return s.RequestPermissionResponse.model_validate({"outcome": {"outcome": "selected", "optionId": "allow"}})
        conn.request_permission = approve
        agent.on_connect(conn)
        await agent.load_session("/local/fleet", SID)
        assert agent.sessions[SID].pending is None
        poll = asyncio.create_task(agent.poll(SID, agent.sessions[SID]))
        await offered.wait()
        assert hq.actions == []
        decision.set()
        await poll
        assert hq.actions == [(SID, "respond", {"revision": "r1", "response": {
            "request_id": "external-permission", "behavior": "allow", "updated_input": {"command": "example"}}})]
    asyncio.run(check())
