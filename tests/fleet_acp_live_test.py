"""Opt-in real fleet acceptance, restricted to owner-reserved isolated sessions.

CC_FLEET_E2E_MANIFEST points at a private JSON manifest with hq_url, optional
hq_token_file, bridge_args, artifact_dir, and cases. Each case must set isolated,
session_id, workdir, identity (exact HQ detail fields), and steps. A step has a
unique marker, prompt, and optionally stop_after_running or an exact expected
permission/question decision. Attempt files prevent automatic write retries.
This is separate from account_acp_transport_test.py's isolated fault injection.
"""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from acp import Client, RequestError, spawn_agent_process
from acp import schema as s

SCRIPT = Path(__file__).parents[1] / "scripts/cc-proxy-acp"


class AcceptanceClient(Client):
    def __init__(self):
        self.updates = []
        self.decisions = []
        self.step = {}
        self.running = asyncio.Event()

    async def session_update(self, session_id, update, **kwargs):
        wire = update.model_dump(by_alias=True, exclude_none=True)
        self.updates.append(wire)
        if wire.get("_meta", {}).get("t3.codes/externalSessionState", {}).get("state") == "running":
            self.running.set()

    async def request_permission(self, options, session_id, tool_call, **kwargs):
        expected = self.step.get("permission")
        if not expected or tool_call.title != expected["title"] or tool_call.raw_input != expected["input"]:
            raise RequestError(-32000, "Unexpected permission in isolated acceptance; no decision sent")
        decision = expected["decision"]
        assert decision in {"allow", "deny"}
        assert decision in {option.option_id for option in options}
        self.decisions.append({"kind": "permission", "decision": decision})
        return s.RequestPermissionResponse.model_validate({"outcome": {"outcome": "selected", "optionId": decision}})

    async def create_elicitation(self, message, mode, **kwargs):
        expected = self.step.get("question")
        if not expected or not isinstance(mode, s.ElicitationFormSessionMode):
            raise RequestError(-32000, "Unexpected question in isolated acceptance; no answer sent")
        schema = mode.requested_schema.model_dump(by_alias=True, exclude_none=True)
        assert schema == expected["schema"]
        decision = expected["decision"]
        self.decisions.append({"kind": "question", "decision": decision})
        if decision == "decline":
            return s.DeclineElicitationResponse()
        if decision == "cancel":
            return s.CancelElicitationResponse()
        assert decision == "accept"
        return s.AcceptElicitationResponse(content=expected["answers"])


def assembled(updates):
    result = {}
    for update in updates:
        if update["sessionUpdate"] in {"user_message_chunk", "agent_message_chunk"}:
            key = (update["sessionUpdate"], update["messageId"])
            result[key] = result.get(key, "") + update["content"]["text"]
    return result


async def run_case(config, case, artifacts):
    assert case["isolated"] is True
    assert "cc-fleet" in case["workdir"], "Only reserved cc-fleet fixture workdirs are eligible"
    assert case["identity"], "An exact owner-provided identity guard is required"
    session_id = case["session_id"]
    token = Path(config["hq_token_file"]).expanduser().read_text().strip() if config.get("hq_token_file") else ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=config["hq_url"], headers=headers, timeout=30) as http:
        async def guarded_detail():
            response = await http.get(f"/api/sessions/{quote(session_id, safe='')}")
            response.raise_for_status()
            row = response.json()
            assert row["id"] == session_id and row.get("alive") is True and row.get("online") is not False
            assert row["workdir"] == case["workdir"]
            assert {key: row.get(key) for key in case["identity"]} == case["identity"]
            return row

        before = await guarded_detail()
        client = AcceptanceClient()
        async with spawn_agent_process(client, sys.executable, str(SCRIPT), *config["bridge_args"],
                                       use_unstable_protocol=True) as (agent, _):
            initialized = await agent.initialize(1, s.ClientCapabilities.model_validate({
                "_meta": {"t3.codes/externalSession": {"version": 1}}, "elicitation": {"form": {}}}))
            assert initialized.protocol_version == 1
            listed, cursor = [], None
            while True:
                page = await agent.list_sessions(cursor=cursor)
                listed.extend(entry.session_id for entry in page.sessions)
                cursor = page.next_cursor
                if not cursor:
                    break
            assert session_id in listed
            await agent.load_session(case["workdir"], session_id, [])
            initial = assembled(client.updates)
            for message in before.get("messages", []):
                if message.get("id") and message.get("role") in {"user", "assistant"} and message.get("text"):
                    role = "user_message_chunk" if message["role"] == "user" else "agent_message_chunk"
                    assert initial.get((role, str(message["id"])), "").startswith(message["text"])
            attachment = {"proof": "real-fleet-acp-read-only", "session_id": session_id,
                          "identity": case["identity"], "native_messages_checked": len(initial),
                          "same_identity": True, "host": case.get("host")}
            attachment_key = hashlib.sha256(session_id.encode()).hexdigest()[:16]
            (artifacts / f"{attachment_key}.attachment.json").write_text(json.dumps(attachment, indent=2) + "\n")
            for step in case["steps"]:
                current = await guarded_detail()
                assert (current.get("activity") or current.get("status")) == "idle"
                marker = step["marker"]
                assert marker in step["prompt"] and marker.isidentifier()
                attempt = artifacts / f"{marker}.attempt.json"
                with attempt.open("x") as file:
                    json.dump({"session_id": session_id, "identity": case["identity"], "marker": marker,
                               "bridge_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest()}, file)
                client.step = step
                client.running.clear()
                client.updates.clear()
                task = asyncio.create_task(agent.prompt(session_id, [s.TextContentBlock(text=step["prompt"])],
                                                       **{"t3.codes/messageId": marker}))
                try:
                    if step.get("stop_after_running"):
                        await asyncio.wait_for(client.running.wait(), 45)
                        await agent.cancel(session_id)
                    result = await asyncio.wait_for(asyncio.shield(task), 180)
                    expected_stop = "cancelled" if step.get("stop_after_running") else "end_turn"
                    assert result.stop_reason == expected_stop
                finally:
                    if not task.done():
                        task.cancel()
                after = await guarded_detail()
                seen = assembled(client.updates)
                if not step.get("stop_after_running"):
                    assert any(marker in text for (role, _), text in seen.items() if role == "agent_message_chunk")
                client.updates.clear()
                await agent.load_session(case["workdir"], session_id, [])
                replay = assembled(client.updates)
                assert all(replay.get(key) == text for key, text in initial.items())
                assert all(replay.get(key, "").startswith(text) for key, text in seen.items())
                report = {"proof": "real-fleet-acp", "session_id": session_id, "identity": case["identity"],
                          "marker": marker, "stop_reason": result.stop_reason, "initial_messages": len(initial),
                          "reloaded_messages": len(replay), "decisions": client.decisions,
                          "same_identity": True, "status_after": after.get("activity") or after.get("status"),
                          "provider": before.get("provider", "claude"), "host": case.get("host")}
                (artifacts / f"{marker}.result.json").write_text(json.dumps(report, indent=2) + "\n")
            await agent.close_session(session_id)
            await guarded_detail()


def test_reserved_fleet_sessions_via_real_acp():
    path = os.environ.get("CC_FLEET_E2E_MANIFEST")
    if not path:
        pytest.skip("Requires an explicit owner-reserved live fixture manifest")
    config = json.loads(Path(path).expanduser().read_text())
    artifacts = Path(config["artifact_dir"]).expanduser()
    artifacts.mkdir(parents=True, exist_ok=True)
    assert config["cases"]
    async def check():
        for case in config["cases"]:
            await run_case(config, case, artifacts)
    asyncio.run(check())


@pytest.mark.parametrize("question", [False, True])
def test_acceptance_harness_against_isolated_protocol(tmp_path, question):
    import runpy

    protocol = runpy.run_path(str(Path(__file__).with_name("account_acp_transport_test.py")))
    fixture = protocol["owner"].__wrapped__()
    owner = next(fixture)
    try:
        owner["row"]["workdir"] = "/isolated/cc-fleet-fixture"
        token = tmp_path / "token"
        token.write_text("isolated-token")
        config = {"hq_url": owner["url"], "bridge_args": ["--hq-url", owner["url"],
            "--mcp-url", owner["url"] + "/mcp", "--mcp-token-file", str(token), "--poll-interval", "0.02",
            "--turn-timeout", "3"]}
        case = {"isolated": True, "session_id": protocol["SID"], "workdir": owner["row"]["workdir"],
                "identity": {"id": protocol["SID"]}, "steps": [{"marker": "ISOLATED_OK", "prompt": "Reply ISOLATED_OK"}]}
        if question:
            owner["control"] = {"request_id": "native-question", "tool_name": "AskUserQuestion",
                "input": {"questions": [{"id": "color", "question": "Which color?",
                    "options": [{"label": "Blue"}, {"label": "Green"}]}]}}
            case["steps"][0]["question"] = {"decision": "accept", "answers": {"answer_0": "Blue"},
                "schema": {"type": "object", "properties": {
                    "answer_0": {"type": "string", "title": "Which color?", "oneOf": [
                        {"const": "Blue", "title": "Blue"}, {"const": "Green", "title": "Green"}]},
                    "answer_0__other": {"type": "string", "title": "Other"}}, "required": []}}
        asyncio.run(run_case(config, case, tmp_path))
        if question:
            assert owner["decisions"][0]["updated_input"]["answers"] == {"color": "Blue"}
        result = json.loads((tmp_path / "ISOLATED_OK.result.json").read_text())
        assert result["same_identity"] and result["reloaded_messages"] == 3
        assert len(owner["delivered"]) == 1
        with pytest.raises(FileExistsError):
            asyncio.run(run_case(config, case, tmp_path))
        assert len(owner["delivered"]) == 1
    finally:
        fixture.close()
