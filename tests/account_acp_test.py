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



def test_discovery_shows_the_owners_title_machine_and_live_state():
    async def check():
        hq = mock_hq(listing=[
            row(id="a", transport="tmux", provider="claude", tmux_session="hq-reply-radar-1",
                title="Reply drafts runway", host="Work / Work", status="running"),
            row(id="b", transport="codex", provider="codex", tmux_session="01a1", title="emulators",
                host="MacBook / Codex", status="idle"),
            row(id="c", transport="tmux", provider="claude", tmux_session="ha-db", title="HA DB",
                host="Client / Client", status="waiting", waiting_on_question=True),
            row(id="d", transport="native", provider="claude", title="dev-mutable-boot",
                host="Remote Control", status="waiting"),
        ])
        agent = FleetAgent(hq, observe=False)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            "_meta": {"t3.codes/externalSession": {"version": 1}}}))
        response = await agent.list_sessions("/local/fleet")
        listed = [(entry.session_id, entry.title,
                   entry.field_meta["t3.codes/externalSessionIdentity"]["connection"]["host"],
                   entry.field_meta["t3.codes/externalSessionState"]) for entry in response.sessions]
        assert listed == [
            ("a", "Reply drafts runway", "Work", {"version": 1, "state": "running"}),
            ("b", "emulators", "MacBook", {"version": 1, "state": "idle"}),
            ("c", "HA DB", "Client", {"version": 1, "state": "requires_action"}),
            ("d", "dev-mutable-boot", "Remote Control", {"version": 1, "state": "requires_action"}),
        ]
        assert hq.actions == []
    asyncio.run(check())


def test_discovery_lists_a_remote_control_session_once_under_its_tmux_pane():
    async def check():
        bridge = "cse_" + "b" * 32
        hq = mock_hq(listing=[
            row(id="pane", transport="tmux", provider="claude", tmux_session="build", host="Work / Work",
                bridge_session_id=bridge),
            row(id="rc", transport="native", provider="claude", tmux_session=bridge, host="Remote Control"),
            row(id="other", transport="native", provider="claude", tmux_session="cse_" + "c" * 32,
                host="Remote Control"),
        ])
        agent = FleetAgent(hq, observe=False)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            "_meta": {"t3.codes/externalSession": {"version": 1}}}))
        response = await agent.list_sessions("/local/fleet")
        assert {entry.session_id: entry.field_meta["t3.codes/externalSessionIdentity"].get("duplicateOf")
                for entry in response.sessions} == {"pane": None, "rc": "pane", "other": None}
        assert hq.actions == []
    asyncio.run(check())


@pytest.mark.parametrize(("changes", "expected"), [
    ({"transport": "tmux", "tmux_session": "build", "title": "build"}, "build"),
    ({"transport": "tmux", "tmux_session": "build", "title": None}, "build"),
    ({"transport": "tmux", "session": "legacy", "title": None}, "legacy"),
    ({"transport": "native", "tmux_session": "build"}, "Existing work"),
    ({"transport": "native", "provider": "codex", "tmux_session": "build"}, "Existing work"),
])
def test_discovery_title_is_the_owners_title_without_transport_labels(changes, expected):
    async def check():
        hq = mock_hq(listing=[row(**changes)])
        response = await FleetAgent(hq, observe=False).list_sessions("/local/fleet")
        assert response.sessions[0].title == expected
        assert response.sessions[0].session_id == SID
        assert response.sessions[0].field_meta is None
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
            assert isinstance(mode, s.ElicitationFormSessionMode)
            assert mode.session_id == SID
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
        assert poll.done()
        decision.set()
        await poll
        await asyncio.wait_for(hq.sent.wait(), 1)
        assert hq.actions == [(SID, "respond", {"revision": "r1", "response": {
            "request_id": "external-permission", "behavior": "allow", "updated_input": {"command": "example"}}})]
    asyncio.run(check())


def test_external_session_v1_metadata_is_explicit_and_observed_state_is_not_a_local_turn():
    async def check():
        hq = mock_hq(row(status="running"), row(status="running"), row(status="idle"),
                     row(status="waiting"), row(native_control={"request_id": "permission"}))
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        initialized = await agent.initialize(2, client_capabilities=s.ClientCapabilities.model_validate({
            "_meta": {"t3.codes/externalSession": {"version": 1}}}))
        assert initialized.protocol_version == 1
        assert initialized.field_meta == {"t3.codes/externalSession": {"version": 1},
                                          "t3.codes/connectionRole": {"version": 1, "role": "discovery"}}
        await agent.load_session("/local/fleet", SID)
        attached = agent.sessions[SID]
        await agent.poll(SID, attached)
        await agent.poll(SID, attached)
        await agent.poll(SID, attached)
        # Test state independently of the user's permission dialog.
        await agent.emit_state(SID, await hq.detail(SID), attached)
        states = [update["_meta"]["t3.codes/externalSessionState"] for _, update in conn.updates
                  if update["sessionUpdate"] == "session_info_update"]
        assert states == [{"version": 1, "state": state} for state in
                          ["running", "idle", "requires_action"]]
        assert attached.pending is None
        assert hq.actions == []
    asyncio.run(check())


def test_old_clients_receive_no_external_state_extension():
    async def check():
        agent = FleetAgent(mock_hq(row(status="running")), observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.initialize(1)
        await agent.load_session("/local/fleet", SID)
        assert all(update["sessionUpdate"] != "session_info_update" for _, update in conn.updates)
    asyncio.run(check())


def test_observation_failure_emits_unavailable_then_actual_recovery_state():
    async def check():
        hq = mock_hq(row(status="running"))
        agent = FleetAgent(hq, observe=False, sleep=no_wait)
        conn = client()
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            "_meta": {"t3.codes/externalSession": {"version": 1}}}))
        await agent.load_session("/local/fleet", SID)
        attached = agent.sessions[SID]
        count = 0
        async def detail(_):
            nonlocal count
            count += 1
            if count == 1:
                raise RequestError(-32000, "offline")
            agent.sessions.pop(SID)
            return row(status="idle")
        hq.detail = detail
        await agent.watch(SID, attached)
        states = [update["_meta"]["t3.codes/externalSessionState"]["state"]
                  for _, update in conn.updates if update["sessionUpdate"] == "session_info_update"]
        assert states == ["running", "unavailable", "idle"]
        assert hq.actions == []
    asyncio.run(check())


def test_codex_user_echo_correlates_only_by_owner_request_id_not_matching_text():
    async def check():
        unrelated = row(provider="codex", status="running", messages=[*row()["messages"],
            {"id": "other-user", "role": "user", "text": "new task", "client_id": "another-request"},
            {"id": "other-answer", "role": "assistant", "text": "Unrelated answer"}])
        complete = row(provider="codex", messages=[*unrelated["messages"],
            {"id": "native-user", "role": "user", "text": "normalized new task", "client_id": "exact-request"},
            {"id": "native-answer", "role": "assistant", "text": "Task complete"}])
        hq = mock_hq(row(provider="codex"), row(provider="codex"), unrelated, complete, complete)
        original_action = hq.action
        async def action(*args, **kwargs):
            await original_action(*args, **kwargs)
            return {"accepted": True, "request_id": "exact-request"}
        hq.action = action
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            "_meta": {"t3.codes/externalSession": {"version": 1}}}))
        await agent.load_session("/local/fleet", SID)
        task = asyncio.create_task(agent.prompt(SID, [s.TextContentBlock(text="new task")],
                                               **{"t3.codes/messageId": "app-user"}))
        await hq.sent.wait()
        attached = agent.sessions[SID]
        await agent.poll(SID, attached)
        assert attached.own_user_key is None and not task.done()
        await agent.poll(SID, attached)
        await agent.poll(SID, attached)
        assert (await task).stop_reason == "end_turn"
        echoes = [update for _, update in conn.updates if update["sessionUpdate"] == "user_message_chunk"]
        assert [update["messageId"] for update in echoes] == ["other-user", "native-user"]
        assert "_meta" not in echoes[0]
        assert echoes[1]["_meta"] == {"t3.codes/messageId": "app-user"}
    asyncio.run(check())


def test_new_external_work_can_be_stopped_after_prior_stop_was_confirmed_idle():
    async def check():
        hq = mock_hq(row(status="running"))
        agent = FleetAgent(hq, observe=False)
        agent.on_connect(client())
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            "_meta": {"t3.codes/externalSession": {"version": 1}}}))
        await agent.load_session("/local/fleet", SID)
        attached = agent.sessions[SID]
        await agent.cancel(SID)
        await agent.cancel(SID)
        assert len(hq.actions) == 1
        await agent.emit_state(SID, row(status="idle"), attached)
        await agent.cancel(SID)
        assert len(hq.actions) == 1
        await agent.emit_state(SID, row(status="running"), attached)
        await agent.cancel(SID)
        assert len(hq.actions) == 2
        assert all(action == "interrupt" for _, action, __ in hq.actions)
    asyncio.run(check())


def test_native_claude_receipt_source_id_correlates_without_rewriting_native_identity():
    async def check():
        complete = row(messages=[*row()["messages"],
            {"id": "native-user", "role": "user", "text": "owner-normalized input"},
            {"id": "native-answer", "role": "assistant", "text": "Task complete"}])
        hq = mock_hq(row(), row(), complete, complete)
        original_action = hq.action
        async def action(*args, **kwargs):
            await original_action(*args, **kwargs)
            return {"accepted": True, "source_message_id": "native-user"}
        hq.action = action
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            "_meta": {"t3.codes/externalSession": {"version": 1}}}))
        await agent.load_session("/local/fleet", SID)
        task = asyncio.create_task(agent.prompt(SID, [s.TextContentBlock(text="new task")],
                                               **{"t3.codes/messageId": "app-user"}))
        await hq.sent.wait()
        await agent.poll(SID, agent.sessions[SID])
        await agent.poll(SID, agent.sessions[SID])
        assert (await task).stop_reason == "end_turn"
        echo = next(update for _, update in conn.updates if update.get("messageId") == "native-user")
        assert echo["_meta"] == {"t3.codes/messageId": "app-user"}
        assert echo["content"]["text"] == "owner-normalized input"
    asyncio.run(check())


def test_bounded_source_history_is_explicit_and_warned_once_per_attachment():
    async def check():
        limited = row(history_truncated=True)
        hq = mock_hq(limited, limited)
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.load_session("/local/fleet", SID)
        await agent.poll(SID, agent.sessions[SID])
        notices = [update for _, update in conn.updates if update["sessionUpdate"] == "notice"]
        assert len(notices) == 1
        assert notices[0]["title"] == "Recent history only"
        assert "Earlier messages are not included" in notices[0]["description"]
        assert hq.actions == []
    asyncio.run(check())


@pytest.mark.parametrize('answers,expected', [
    ({'answer_0': 'Option A'}, 'Option A'),
    ({'answer_0__other': 'Custom answer'}, 'Custom answer'),
    ({'answer_0': ['Option A', 'Option B'], 'answer_0__other': 'Custom answer'}, 'Option A, Option B, Custom answer'),
])
def test_native_question_choices_and_other_use_supported_form_schema(answers, expected):
    async def check():
        hq = mock_hq()
        agent = FleetAgent(hq, observe=False)
        conn = client()
        multi = isinstance(answers.get('answer_0'), list)
        async def choose(message, mode, **kwargs):
            schema = mode.requested_schema.model_dump(by_alias=True, exclude_none=True)
            assert schema['required'] == []
            value = schema['properties']['answer_0']
            options = value['items']['anyOf'] if multi else value['oneOf']
            assert [option['const'] for option in options] == ['Option A', 'Option B'] + (
                ['__cc_fleet_other__'] if multi else [])
            assert options[0]['description'] == 'First choice'
            assert schema['properties']['answer_0__other']['type'] == 'string'
            return s.AcceptElicitationResponse(content=answers)
        conn.create_elicitation = choose
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({'elicitation': {'form': {}}}))
        control = {'request_id': 'question', 'tool_name': 'AskUserQuestion', 'input': {'questions': [
            {'question': 'Pick a value', 'multiSelect': multi,
             'options': [{'label': 'Option A', 'description': 'First choice'}, {'label': 'Option B'}]}]}}
        await agent.handle_control(SID, row(native_control=control), bridge['Attached']())
        assert hq.actions[0][2]['response']['updated_input']['answers'] == {'Pick a value': expected}
    asyncio.run(check())


@pytest.mark.parametrize('answers', [{'answer_0': 'not-offered'}, {'answer_0': True},
                                      {'answer_0': 'A', 'answer_0__other': 'ambiguous'}, {}])
def test_invalid_native_choice_is_rejected_without_answering_the_owner(answers):
    async def check():
        hq = mock_hq()
        agent = FleetAgent(hq, observe=False)
        conn = client()
        async def choose(*args, **kwargs):
            return s.AcceptElicitationResponse(content=answers)
        conn.create_elicitation = choose
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({'elicitation': {'form': {}}}))
        control = {'request_id': 'question', 'tool_name': 'AskUserQuestion', 'input': {'questions': [
            {'question': 'Pick', 'options': [{'label': 'A'}]}]}}
        attached = bridge['Attached']()
        await agent.handle_control(SID, row(native_control=control), attached)
        assert hq.actions == []
        assert not attached.controls
        assert conn.updates[-1][1]['title'] == 'Answer needs correction'
        async def correct(*args, **kwargs):
            return s.AcceptElicitationResponse(content={'answer_0': 'A'})
        conn.create_elicitation = correct
        await agent.handle_control(SID, row(native_control=control), attached)
        assert len(hq.actions) == 1
        assert hq.actions[0][2]['response']['updated_input']['answers'] == {'Pick': 'A'}
    asyncio.run(check())


@pytest.mark.parametrize('labels,selected,other,expected', [
    (['A'], ['__cc_fleet_other__'], 'custom', 'custom'),
    (['__cc_fleet_other__'], ['__cc_fleet_other__', '__cc_fleet_other___'], 'custom', '__cc_fleet_other__, custom'),
    (['A'], ['__cc_fleet_other__'], '', None),
])
def test_multi_select_other_is_explicit_and_cannot_collide_with_native_options(labels, selected, other, expected):
    async def check():
        hq = mock_hq()
        agent = FleetAgent(hq, observe=False)
        conn = client()
        async def choose(message, mode, **kwargs):
            form = mode.requested_schema.model_dump(by_alias=True, exclude_none=True)
            options = form['properties']['answer_0']['items']['anyOf']
            assert len({option['const'] for option in options}) == len(labels) + 1
            assert options[-1]['const'] not in labels
            return s.AcceptElicitationResponse(content={'answer_0': selected, 'answer_0__other': other})
        conn.create_elicitation = choose
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({'elicitation': {'form': {}}}))
        control = {'request_id': 'question', 'tool_name': 'AskUserQuestion', 'input': {'questions': [
            {'question': 'Pick', 'multiSelect': True, 'options': [{'label': label} for label in labels]}]}}
        attached = bridge['Attached']()
        await agent.handle_control(SID, row(native_control=control), attached)
        if expected is None:
            assert hq.actions == [] and not attached.controls
        else:
            assert hq.actions[0][2]['response']['updated_input']['answers'] == {'Pick': expected}
    asyncio.run(check())


@pytest.mark.parametrize("action", ["send", "interrupt", "respond", "select", "key"])
def test_archived_history_is_readable_but_every_action_is_rejected_before_post(action):
    async def check():
        requests = []
        archived = row(provider="codex", transport="codex", alive=False, online=True, lifecycle="ended", read_only=True, status="ended",
                       capabilities={"send": False, "interrupt": False, "respond": False})

        def handle(request):
            requests.append(request.method)
            return httpx.Response(200, json=archived)

        hq = HQ("http://hq.example", "toy-secret", transport=httpx.MockTransport(handle))
        try:
            assert await hq.detail(SID) == archived
            with pytest.raises(bridge["ActionRejected"], match="offline or ended"):
                await hq.action(SID, action)
            assert requests == ["GET", "GET"]
        finally:
            await hq.http.aclose()
    asyncio.run(check())


def test_archived_history_cannot_retarget_an_existing_session():
    async def check():
        archived = row(id="another-session", alive=False, lifecycle="ended", read_only=True)
        hq = HQ("http://hq.example", "toy-secret",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=archived)))
        try:
            with pytest.raises(RequestError, match="different session identity"):
                await hq.detail(SID)
        finally:
            await hq.http.aclose()
    asyncio.run(check())


def test_archived_transition_cancels_existing_permission_without_answering_it():
    async def check():
        current = row(provider="codex", transport="codex", native_control={"request_id": "pending-permission", "tool_name": "Bash",
                                     "input": {"command": "true"}})
        methods = []
        requested = asyncio.Event()

        def handle(request):
            methods.append(request.method)
            return httpx.Response(200, json=current)

        async def wait_for_permission(*args, **kwargs):
            requested.set()
            await asyncio.Future()

        hq = HQ("http://hq.example", "toy-secret", transport=httpx.MockTransport(handle))
        agent = FleetAgent(hq, observe=False)
        conn = client()
        conn.request_permission = wait_for_permission
        agent.on_connect(conn)
        agent.external_session = True
        try:
            await agent.load_session("/local/fleet", SID)
            attached = agent.sessions[SID]
            await agent.poll(SID, attached)
            await requested.wait()
            permission = attached.control_task
            current.update(alive=False, online=True, lifecycle="ended", read_only=True, status="ended")
            await agent.poll(SID, attached)
            assert permission.cancelled()
            assert attached.control_task is None and attached.control_key is None
            assert attached.state == "unavailable"
            assert methods == ["GET", "GET", "GET"]
        finally:
            await agent.close_session(SID)
            await hq.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize("changes", [{"provider": None}, {"transport": "tmux"},
    {"read_only": False}, {"alive": True}, {"online": False}, {"lifecycle": "unreachable"},
    {"revision": None}])
def test_archived_history_requires_the_complete_codex_owner_contract(changes):
    async def check():
        archived = row(provider="codex", transport="codex", read_only=True,
                       alive=False, online=True, lifecycle="ended")
        archived.update(changes)
        hq = HQ("http://hq.example", "toy-secret",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=archived)))
        try:
            with pytest.raises(RequestError):
                await hq.detail(SID)
        finally:
            await hq.http.aclose()
    asyncio.run(check())


@pytest.mark.parametrize(('provider', 'transport', 'engine'), [
    ('claude', 'tmux', 'claude-code'),
    ('claude', 'native', 'claude-code'),
    ('codex', 'codex', 'codex'),
])
def test_external_identity_discovery_preserves_route_and_uses_owner_engine(provider, transport, engine):
    async def check():
        hq = mock_hq(listing=[row(provider=provider, transport=transport, title='Misleading other engine',
                                 model='gpt-test', account='must-not-be-exposed')])
        agent = FleetAgent(hq, observe=False)
        initialized = await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            '_meta': {'t3.codes/externalSession': {'version': 1}}}))
        assert initialized.field_meta['t3.codes/connectionRole'] == {'version': 1, 'role': 'discovery'}
        response = await agent.list_sessions('/local/fleet')
        entry = response.sessions[0]
        assert entry.session_id == SID
        identity = entry.field_meta['t3.codes/externalSessionIdentity']
        assert identity['engine'] == engine
        assert identity['connection'] == {'kind': 'fleet', 'host': 'node-one', 'transport': transport}
        assert identity['model'] == 'gpt-test'
        assert 'account' not in identity
        assert identity['capabilities'] == {
            'send': transport == 'tmux', 'interrupt': transport == 'tmux', 'respond': False,
            'setModel': False, 'setPermissionMode': False, 'terminal': False}
        assert hq.actions == []
    asyncio.run(check())


@pytest.mark.parametrize('changes', [
    {'provider': None}, {'provider': 'unknown'}, {'provider': 'codex', 'transport': 'native'},
    {'provider': 'claude', 'transport': 'codex'}, {'transport': 'ssh'}, {'host': '\ninvalid'},
])
def test_external_identity_never_guesses_unverified_engine_or_connection(changes):
    async def check():
        hq = mock_hq(listing=[row(**{'provider': 'claude', **changes})])
        agent = FleetAgent(hq, observe=False)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            '_meta': {'t3.codes/externalSession': {'version': 1}}}))
        entry = (await agent.list_sessions('/local/fleet')).sessions[0]
        assert 't3.codes/externalSessionIdentity' not in (entry.field_meta or {})
        assert entry.session_id == SID
        assert hq.actions == []
    asyncio.run(check())


def test_external_identity_detail_updates_without_state_change_and_disables_archive_controls():
    async def check():
        native_id = '11111111-1111-4111-8111-111111111111'
        first = row(provider='codex', transport='codex', codex_thread_id=native_id, model='model-one',
                    capabilities={'send': True, 'interrupt': False, 'respond': False, 'set_model': True})
        second = {**first, 'model': 'model-two', 'capabilities': {'send': False, 'interrupt': True, 'respond': True}}
        archive = {**second, 'read_only': True, 'alive': False, 'lifecycle': 'ended'}
        hq = mock_hq(first, second)
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            '_meta': {'t3.codes/externalSession': {'version': 1}}}))
        loaded = await agent.load_session('/local/fleet', SID)
        identity = loaded.field_meta['t3.codes/externalSessionIdentity']
        assert identity['nativeSessionId'] == native_id
        assert identity['model'] == 'model-one'
        assert identity['capabilities']['send'] is True
        assert identity['capabilities']['setModel'] is False
        await agent.poll(SID, agent.sessions[SID])
        await agent.emit_state(SID, archive, agent.sessions[SID])
        updates = [update['_meta'] for _, update in conn.updates if update['sessionUpdate'] == 'session_info_update']
        identities = [update['t3.codes/externalSessionIdentity'] for update in updates]
        assert [identity['model'] for identity in identities] == ['model-one', 'model-two', 'model-two']
        assert updates[0]['t3.codes/externalSessionState'] == updates[1]['t3.codes/externalSessionState']
        assert identities[1]['capabilities']['interrupt'] is True
        assert all(value is False for value in identities[2]['capabilities'].values())
        assert list(agent.sessions) == [SID]
        assert hq.actions == []
    asyncio.run(check())


def test_external_identity_observation_failure_disables_controls_and_recovery_restores_them():
    async def check():
        detail = row(provider='claude', transport='native', claude_session_id='exact-native-owner',
                     model='model-current', capabilities={'send': True, 'interrupt': True, 'respond': True,
                                                          'set_model': True, 'set_permission_mode': True})
        agent = FleetAgent(mock_hq(detail), observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            '_meta': {'t3.codes/externalSession': {'version': 1}}}))
        await agent.resume_session('/local/fleet', SID)
        attached = agent.sessions[SID]
        await agent.emit_state(SID, None, attached)
        await agent.emit_state(SID, detail, attached)
        identities = [u['_meta']['t3.codes/externalSessionIdentity'] for _, u in conn.updates
                      if u['sessionUpdate'] == 'session_info_update']
        assert [i['capabilities']['send'] for i in identities] == [True, False, True]
        assert all(not i['capabilities']['setModel'] and not i['capabilities']['setPermissionMode']
                   and not i['capabilities']['terminal'] for i in identities)
        assert all(i['nativeSessionId'] == 'exact-native-owner' for i in identities)
    asyncio.run(check())


def test_external_identity_does_not_leak_invalid_optional_metadata_or_opt_out_extension():
    async def check():
        detail = row(provider='claude', transport='tmux', model={'invalid': 'model'}, claude_session_id='\ninvalid')
        hq = mock_hq(detail, listing=[detail])
        agent = FleetAgent(hq, observe=False)
        conn = client()
        agent.on_connect(conn)
        await agent.initialize(1)
        assert (await agent.list_sessions('/local/fleet')).sessions[0].field_meta is None
        assert (await agent.load_session('/local/fleet', SID)).field_meta is None
        assert all(u['sessionUpdate'] != 'session_info_update' for _, u in conn.updates)
        identity = bridge['external_identity'](detail)
        assert 'model' not in identity and 'nativeSessionId' not in identity
    asyncio.run(check())


@pytest.mark.parametrize('owner_value', [False, 'true', 1])
def test_external_identity_explicit_owner_denial_overrides_legacy_tmux_support(owner_value):
    async def check():
        hq = mock_hq(listing=[row(provider='claude', transport='tmux', menu={'signature': 'pending'},
                                 capabilities={'send': owner_value, 'interrupt': owner_value, 'respond': owner_value})])
        agent = FleetAgent(hq, observe=False)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            '_meta': {'t3.codes/externalSession': {'version': 1}}}))
        entry = (await agent.list_sessions('/local/fleet')).sessions[0]
        capabilities = entry.field_meta['t3.codes/externalSessionIdentity']['capabilities']
        assert not any(capabilities.values())
        assert hq.actions == []
    asyncio.run(check())


@pytest.mark.parametrize('capabilities', [None, {}, [], 'invalid'])
def test_external_identity_malformed_owner_capabilities_are_not_legacy_absence(capabilities):
    async def check():
        hq = mock_hq(listing=[row(provider='claude', transport='tmux', capabilities=capabilities)])
        agent = FleetAgent(hq, observe=False)
        await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
            '_meta': {'t3.codes/externalSession': {'version': 1}}}))
        entry = (await agent.list_sessions('/local/fleet')).sessions[0]
        assert not any(entry.field_meta['t3.codes/externalSessionIdentity']['capabilities'].values())
        assert hq.actions == []
    asyncio.run(check())


@pytest.mark.parametrize(('field', 'limit', 'output'), [
    ('host', 256, None), ('model', 256, 'model'), ('claude_session_id', 1024, 'nativeSessionId'),
])
def test_external_identity_text_bounds_match_shared_contract(field, limit, output):
    async def check():
        for length in (limit, limit + 1):
            value = 'x' * length
            hq = mock_hq(listing=[row(provider='claude', transport='tmux', **{field: value})])
            agent = FleetAgent(hq, observe=False)
            await agent.initialize(1, client_capabilities=s.ClientCapabilities.model_validate({
                '_meta': {'t3.codes/externalSession': {'version': 1}}}))
            entry = (await agent.list_sessions('/local/fleet')).sessions[0]
            identity = (entry.field_meta or {}).get('t3.codes/externalSessionIdentity')
            if output is None:
                assert (identity is not None) == (length <= limit)
            else:
                assert identity is not None
                assert identity.get(output) == (value if length <= limit else None)
            assert hq.actions == []
    asyncio.run(check())
