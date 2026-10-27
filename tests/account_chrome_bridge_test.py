"""Native Chrome wire aggregation with toy accounts and in-memory transports."""

import asyncio
import json
import runpy
from pathlib import Path
from uuid import uuid4

import pytest
from mitmproxy import http, websocket
from mitmproxy.test import tflow

bridge = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts/account_chrome_bridge.py")
)


def flow_for(account=None):
    flow = tflow.tflow()
    flow.request = http.Request.make(
        "GET",
        f"https://bridge.claudeusercontent.com/chrome/{account or uuid4()}",
        headers={"Upgrade": "websocket"},
    )
    flow.websocket = websocket.WebSocketData()
    return flow


class FakeSocket:
    def __init__(self, rows, *, handshake="waiting", silent=False):
        self.rows = rows
        self.silent = silent
        self.sent = []
        self.queue = asyncio.Queue()
        self.queue.put_nowait(json.dumps({"type": handshake}))
        self.closed = False

    async def write_message(self, raw):
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame["type"] == "list_extensions" and not self.silent:
            self.queue.put_nowait(
                json.dumps({"type": "extensions_list", "extensions": self.rows})
            )

    async def read_message(self):
        return await self.queue.get()

    def close(self):
        self.closed = True
        self.queue.put_nowait(None)


class Harness:
    def __init__(
        self,
        *,
        source=None,
        side=None,
        fail=False,
        silent=False,
        selected=None,
        label=None,
    ):
        self.source = (
            source
            if source is not None
            else [{"deviceId": "browser-a", "name": "A", "osPlatform": "Linux"}]
        )
        self.socket = FakeSocket(
            side
            if side is not None
            else [{"deviceId": "browser-b", "name": "B", "osPlatform": "macOS"}],
            silent=silent,
        )
        self.sent = []
        self.opened = []
        self.fail = fail
        self.flow = flow_for()
        self.client_rows = []
        self.session = None

        async def connect(request, **options):
            self.opened.append((request.url, options))
            if self.fail:
                raise OSError("toy connection refused")
            if self.socket.closed:
                self.socket = FakeSocket(self.socket.rows, silent=self.socket.silent)
            return self.socket

        def inject(flow, client, raw):
            frame = json.loads(raw)
            self.sent.append((client, frame))
            if not client and frame["type"] == "list_extensions":
                self.session.incoming(
                    "main", {"type": "extensions_list", "extensions": self.source}
                )
            if client and frame["type"] == "extensions_list":
                self.client_rows = frame["extensions"]

        self.mux = bridge["ChromeBridge"](
            ["main", "work"],
            lambda alias: str(uuid4()),
            lambda alias: f"token-{alias}",
            inject,
            connect,
            label=label,
        )
        self.session = self.mux.start(self.flow, "main", selected)
        self.session.incoming("main", {"type": "paired"})

    async def discover(self):
        self.session.outgoing({"type": "list_extensions"})
        await self.session.discovery
        return self.client_rows

    def row(self, alias):
        return next(
            row for row in self.client_rows if row["name"].startswith(alias + ":")
        )

    async def close(self):
        self.mux.close()
        await asyncio.sleep(0)


def test_merges_native_rosters_and_keeps_device_ids_stable_and_valid():
    async def run():
        harness = Harness(
            source=[{"deviceId": "same-id", "name": "Source"}],
            side=[{"deviceId": "same-id", "name": "Side"}],
        )
        try:
            rows = await harness.discover()
            assert {row["name"] for row in rows} == {"main: Source", "work: Side"}
            assert len({row["deviceId"] for row in rows}) == 2
            assert all(bridge["DEVICE"].fullmatch(row["deviceId"]) for row in rows)
            assert await harness.discover() == rows
            assert harness.flow.metadata["chrome_account_statuses"] == {
                "main": "available",
                "work": "available",
            }
            assert harness.socket.sent[0] == {
                "type": "connect",
                "client_type": "claude-code",
                "oauth_token": "token-work",
            }
        finally:
            await harness.close()

    asyncio.run(run())


def test_routes_side_and_source_calls_without_changing_native_payloads():
    async def run():
        harness = Harness()
        try:
            await harness.discover()
            opaque = {
                "nested": {"deviceId": "leave-this-alone"},
                "file": "native-preprocessed",
            }
            side = {
                "type": "tool_call",
                "tool_use_id": "call-side",
                "target_device_id": harness.row("work")["deviceId"],
                "tool": "file_upload",
                "args": opaque,
                "permission_mode": "ask",
            }
            assert harness.session.outgoing(side) is None
            await asyncio.sleep(0)
            assert harness.socket.sent[-1] == {**side, "target_device_id": "browser-b"}
            assert harness.socket.sent[-1]["args"] == opaque
            assert side["target_device_id"] == harness.row("work")["deviceId"]
            source = {
                **side,
                "tool_use_id": "call-main",
                "target_device_id": harness.row("main")["deviceId"],
            }
            assert harness.session.outgoing(source) == {
                **source,
                "target_device_id": "browser-a",
            }
            result = {
                "type": "tool_result",
                "tool_use_id": "call-side",
                "content": [{"type": "text", "text": "opaque"}],
            }
            assert harness.session.incoming("main", result) is None
            assert harness.session.incoming("work", result) is result
        finally:
            await harness.close()

    asyncio.run(run())


@pytest.mark.parametrize("target", [None, "forged-id", "browser-a", "work__browser-b"])
def test_unknown_or_unselected_targets_never_fall_back(target):
    async def run():
        harness = Harness()
        try:
            await harness.discover()
            before = list(harness.socket.sent)
            assert (
                harness.session.outgoing(
                    {
                        "type": "tool_call",
                        "tool_use_id": "refused",
                        "target_device_id": target,
                    }
                )
                is None
            )
            await asyncio.sleep(0)
            assert harness.socket.sent == before
            assert harness.sent[-1][1]["type"] == "tool_result"
            assert harness.sent[-1][1]["is_error"] is True
            assert not harness.session.calls
        finally:
            await harness.close()

    asyncio.run(run())


def test_permission_replies_are_bound_to_the_original_account_and_call():
    async def run():
        harness = Harness()
        try:
            await harness.discover()
            target = harness.row("work")["deviceId"]
            harness.session.outgoing(
                {"type": "tool_call", "tool_use_id": "c", "target_device_id": target}
            )
            original = {
                "type": "permission_request",
                "tool_use_id": "c",
                "request_id": "p",
                "prompt": "native permission text",
            }
            assert harness.session.incoming("main", original) is None
            request = harness.session.incoming("work", original)
            assert request["request_id"] != "p"
            assert request["prompt"] == original["prompt"]
            harness.session.outgoing(
                {
                    "type": "permission_response",
                    "request_id": request["request_id"],
                    "target_device_id": target,
                    "allowed": True,
                }
            )
            await asyncio.sleep(0)
            assert harness.socket.sent[-1] == {
                "type": "permission_response",
                "request_id": "p",
                "target_device_id": "browser-b",
                "allowed": True,
            }
            request = harness.session.incoming("work", {**original, "request_id": "p2"})
            before = list(harness.socket.sent)
            harness.session.outgoing(
                {
                    "type": "permission_response",
                    "request_id": request["request_id"],
                    "target_device_id": harness.row("main")["deviceId"],
                    "allowed": True,
                }
            )
            await asyncio.sleep(0)
            assert harness.socket.sent == before
        finally:
            await harness.close()

    asyncio.run(run())


def test_failed_account_is_not_an_empty_account_and_never_receives_calls():
    async def run():
        harness = Harness(fail=True)
        try:
            rows = await harness.discover()
            assert [row["name"] for row in rows] == ["main: A"]
            assert (
                harness.flow.metadata["chrome_account_statuses"]["work"]
                == "unavailable"
            )
            assert any(
                frame.get("error") == "Chrome accounts unavailable: work"
                for _, frame in harness.sent
            )
            harness.session.outgoing(
                {
                    "type": "tool_call",
                    "tool_use_id": "bad",
                    "target_device_id": bridge["public_id"]("work", "browser-b"),
                }
            )
            assert not harness.session.calls
        finally:
            await harness.close()

    asyncio.run(run())


def test_discovery_timeout_clears_previous_roster_and_can_retry(monkeypatch):
    monkeypatch.setitem(
        bridge["BridgeSession"].discover.__globals__, "DISCOVERY_TIMEOUT", 0.02
    )

    async def run():
        harness = Harness()
        try:
            await harness.discover()
            harness.socket.silent = True
            rows = await harness.discover()
            assert [row["name"] for row in rows] == ["main: A"]
            assert (
                harness.flow.metadata["chrome_account_statuses"]["work"]
                == "unavailable"
            )
            harness.socket.silent = False
            assert len(await harness.discover()) == 2
        finally:
            await harness.close()

    asyncio.run(run())


def test_explicit_other_account_hides_main_and_routes_only_to_selected():
    async def run():
        harness = Harness(selected="work")
        try:
            rows = await harness.discover()
            assert [row["name"] for row in rows] == ["work: B"]
            assert (
                harness.session.incoming(
                    "main", {"type": "peer_connected", "deviceId": "browser-a"}
                )
                is None
            )
            harness.session.outgoing(
                {
                    "type": "tool_call",
                    "tool_use_id": "bad",
                    "target_device_id": bridge["public_id"]("main", "browser-a"),
                }
            )
            assert not harness.session.calls
        finally:
            await harness.close()

    asyncio.run(run())


def test_secondary_disconnect_removes_routes_and_fails_owned_calls():
    async def run():
        harness = Harness()
        try:
            await harness.discover()
            target = harness.row("work")["deviceId"]
            harness.session.outgoing(
                {
                    "type": "tool_call",
                    "tool_use_id": "pending",
                    "target_device_id": target,
                }
            )
            await asyncio.sleep(0)
            assert any(
                frame.get("tool_use_id") == "pending" for frame in harness.socket.sent
            )
            harness.socket.queue.put_nowait(None)
            await asyncio.sleep(0)
            assert target not in harness.session.routes
            assert "pending" not in harness.session.calls
            result = next(
                frame
                for _, frame in harness.sent
                if frame.get("tool_use_id") == "pending" and frame.get("is_error")
            )
            assert "outcome is unknown" in result["content"][0]["text"]
            assert "No action ran" not in result["content"][0]["text"]
            assert (
                harness.flow.metadata["chrome_account_statuses"]["main"] == "available"
            )
        finally:
            await harness.close()
        assert harness.socket.closed
        assert not harness.mux.sessions

    asyncio.run(run())


def test_injected_frames_do_not_loop_and_close_cancels_owned_tasks():
    async def run():
        harness = Harness()
        try:
            await harness.discover()
            message = websocket.WebSocketMessage(
                1,
                False,
                b'{"type":"peer_connected","deviceId":"do-not-rewrite"}',
                injected=True,
            )
            harness.flow.websocket.messages.append(message)
            assert not harness.mux.message(harness.flow)
            assert (
                message.content
                == b'{"type":"peer_connected","deviceId":"do-not-rewrite"}'
            )
            tasks = list(harness.session.tasks)
            harness.mux.end(harness.flow)
            await asyncio.gather(*tasks, return_exceptions=True)
            assert all(task.done() for task in tasks)
        finally:
            await harness.close()

    asyncio.run(run())


def test_routes_external_results_pairing_events_and_notifications():
    async def run():
        harness = Harness()
        try:
            await harness.discover()
            target = harness.row("work")["deviceId"]
            harness.session.outgoing(
                {
                    "type": "external_message",
                    "requestId": "e",
                    "target_device_id": target,
                    "payload": {"opaque": True},
                }
            )
            result = {
                "type": "external_message_result",
                "requestId": "e",
                "ok": True,
                "response": [1, 2],
            }
            assert harness.session.incoming("main", result) is None
            assert harness.session.incoming("work", result) is result
            harness.session.outgoing({"type": "pairing_request", "request_id": "pair"})
            assert (
                harness.session.incoming(
                    "work",
                    {
                        "type": "pairing_response",
                        "request_id": "wrong",
                        "device_id": "browser-b",
                    },
                )
                is None
            )
            paired = harness.session.incoming(
                "work",
                {
                    "type": "pairing_response",
                    "request_id": "pair",
                    "device_id": "browser-b",
                    "name": "B",
                },
            )
            assert paired["device_id"] == target and paired["name"] == "work: B"
            event = {
                "type": "notification",
                "method": "vendor/native",
                "params": {"opaque": True},
            }
            assert harness.session.incoming("work", event) is event
        finally:
            await harness.close()

    asyncio.run(run())


def test_friendly_names_apply_to_discovery_and_native_pairing():
    async def run():
        harness = Harness(
            label=lambda alias, device, fallback: f"vm-{alias} - user@example.com"
        )
        try:
            rows = await harness.discover()
            assert {row["name"] for row in rows} == {
                "vm-main - user@example.com",
                "vm-work - user@example.com",
            }
            harness.session.outgoing({"type": "pairing_request", "request_id": "pair"})
            paired = harness.session.incoming(
                "work",
                {
                    "type": "pairing_response",
                    "request_id": "pair",
                    "device_id": "browser-b",
                },
            )
            assert paired["name"] == "vm-work - user@example.com"
        finally:
            await harness.close()

    asyncio.run(run())


def test_tornado_transport_runs_native_permission_roundtrip_on_a_real_websocket():
    from tornado.httpclient import HTTPRequest
    from tornado.httpserver import HTTPServer
    from tornado.netutil import bind_sockets
    from tornado.web import Application
    from tornado.websocket import WebSocketHandler, websocket_connect

    async def run():
        received = []
        peers = []
        permission = asyncio.Event()
        completed = asyncio.Event()

        class Handler(WebSocketHandler):
            def open(self):
                peers.append(self)

            def on_message(self, raw):
                frame = json.loads(raw)
                received.append(frame)
                if frame["type"] == "connect":
                    assert frame["oauth_token"] == "token-work"
                    self.write_message({"type": "waiting"})
                elif frame["type"] == "list_extensions":
                    self.write_message(
                        {
                            "type": "extensions_list",
                            "extensions": [{"deviceId": "browser-b"}],
                        }
                    )
                elif frame["type"] == "tool_call":
                    self.write_message(
                        {
                            "type": "permission_request",
                            "tool_use_id": frame["tool_use_id"],
                            "request_id": "permission-native",
                            "prompt": "Allow native operation?",
                        }
                    )
                elif frame["type"] == "permission_response":
                    assert frame["target_device_id"] == "browser-b"
                    assert (
                        frame["request_id"] == "permission-native"
                        and frame["allowed"] is True
                    )
                    self.write_message(
                        {
                            "type": "tool_result",
                            "tool_use_id": "roundtrip",
                            "content": [{"type": "text", "text": "native-result"}],
                        }
                    )

        sockets = bind_sockets(0, "127.0.0.1")
        port = sockets[0].getsockname()[1]
        server = HTTPServer(Application([(r"/chrome/.*", Handler)]))
        server.add_sockets(sockets)
        harness = Harness()

        async def connect(request, **kwargs):
            assert request.url.startswith("wss://bridge.claudeusercontent.com/chrome/")
            return await websocket_connect(
                HTTPRequest(f"ws://127.0.0.1:{port}/chrome/toy"), **kwargs
            )

        original_inject = harness.session.inject

        def inject(flow, client, raw):
            original_inject(flow, client, raw)
            frame = json.loads(raw)
            if client and frame.get("type") == "permission_request":
                permission.set()
            if client and frame.get("type") == "tool_result":
                completed.set()

        harness.session.connect = connect
        harness.session.inject = inject
        try:
            rows = await harness.discover()
            assert {row["name"] for row in rows} == {"main: A", "work: Browser 1"}
            target = harness.row("work")["deviceId"]
            harness.session.outgoing(
                {
                    "type": "tool_call",
                    "tool_use_id": "roundtrip",
                    "target_device_id": target,
                    "tool": "read_page",
                    "args": {"tabId": 7},
                }
            )
            await asyncio.wait_for(permission.wait(), 2)
            request = next(
                frame
                for client, frame in harness.sent
                if client and frame.get("type") == "permission_request"
            )
            harness.session.outgoing(
                {
                    "type": "permission_response",
                    "request_id": request["request_id"],
                    "target_device_id": target,
                    "allowed": True,
                }
            )
            await asyncio.wait_for(completed.wait(), 2)
            assert harness.sent[-1][1]["content"] == [
                {"type": "text", "text": "native-result"}
            ]
            assert not harness.session.calls and not harness.session.permissions
        finally:
            await harness.close()
            for peer in peers:
                peer.close()
            server.stop()
            await server.close_all_connections()

    asyncio.run(run())
