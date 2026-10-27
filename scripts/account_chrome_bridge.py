"""Multiplex native Chrome bridge envelopes inside the account proxy's listener."""

import asyncio
import json
import re
import ssl
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

from tornado.httpclient import HTTPRequest
from tornado.websocket import websocket_connect

HOST = "bridge.claudeusercontent.com"
DEVICE = re.compile(r"[A-Za-z0-9-]{1,64}\Z")
DISCOVERY_TIMEOUT = 2.5


def public_id(alias, device):
    return str(uuid5(NAMESPACE_URL, f"claude-account-chrome:{alias}:{device}"))


@dataclass(frozen=True)
class DeviceRoute:
    alias: str
    native: str


@dataclass
class AccountChannel:
    alias: str
    socket: object = None
    task: object = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    discovery: object = None
    rows: dict = field(default_factory=dict)
    available: bool = False
    error: bool = False


class BridgeSession:
    def __init__(
        self,
        flow,
        main,
        aliases,
        identity,
        token,
        inject,
        connect=websocket_connect,
        label=None,
    ):
        self.flow = flow
        self.main = main
        self.aliases = tuple(aliases)
        self.identity = identity
        self.token = token
        self.inject = inject
        self.connect = connect
        self.label = label or (lambda alias, device, fallback: f"{alias}: {fallback}")
        self.channels = {alias: AccountChannel(alias) for alias in self.aliases}
        self.routes = {}
        self.calls = {}
        self.permissions = {}
        self.external = {}
        self.pairings = {}
        self.tasks = set()
        self.discovery = None
        self.closed = False
        for alias in self.aliases:
            if alias != main:
                self.ensure_channel(alias)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def emit(self, payload):
        if not self.closed:
            self.inject(self.flow, True, json.dumps(payload).encode())

    def status(self):
        self.flow.metadata["chrome_account_statuses"] = {
            alias: "available"
            if channel.available and not channel.error
            else "unavailable"
            for alias, channel in self.channels.items()
        }

    def route(self, alias, device):
        if not isinstance(device, str) or not DEVICE.fullmatch(device):
            raise ValueError("Invalid native browser identity")
        exported = public_id(alias, device)
        route = DeviceRoute(alias, device)
        if exported in self.routes and self.routes[exported] != route:
            raise ValueError("Browser identity collision")
        self.routes[exported] = route
        return exported

    def ensure_channel(self, alias):
        channel = self.channels[alias]
        if channel.task is None or channel.task.done():
            channel.ready.clear()
            channel.task = self.spawn(self.serve_channel(channel))
        return channel

    async def serve_channel(self, channel):
        heartbeat = None
        try:
            account_id = self.identity(channel.alias)
            access = self.token(channel.alias)
            request = HTTPRequest(
                f"wss://{HOST}/chrome/{account_id}",
                connect_timeout=2,
                request_timeout=2,
                ssl_options=ssl.create_default_context(),
            )
            channel.socket = await self.connect(
                request,
                ping_interval=15,
                ping_timeout=15,
                max_message_size=64 * 1024 * 1024,
            )
            await channel.socket.write_message(
                json.dumps(
                    {
                        "type": "connect",
                        "client_type": "claude-code",
                        "oauth_token": access,
                    }
                )
            )
            async with asyncio.timeout(2):
                raw = await channel.socket.read_message()
            first = json.loads(raw)
            if not isinstance(first, dict) or first.get("type") not in {
                "paired",
                "waiting",
            }:
                raise ValueError("Chrome account authentication failed")
            channel.available = True
            channel.ready.set()
            self.status()
            heartbeat = self.spawn(self.keepalive(channel))
            while (raw := await channel.socket.read_message()) is not None:
                frame = json.loads(raw)
                if not isinstance(frame, dict):
                    raise TypeError("Invalid Chrome bridge frame")
                if frame.get("type") == "ping":
                    await self.send(channel.alias, {"type": "pong"})
                elif frame.get("type") not in {"pong", "paired", "waiting"}:
                    forwarded = self.incoming(channel.alias, frame)
                    if forwarded is not None:
                        self.emit(forwarded)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - native payloads and credentials must not reach logs
            channel.error = True
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            if channel.socket is not None:
                channel.socket.close()
                channel.socket = None
            self.unavailable(channel.alias)
            channel.ready.set()

    async def keepalive(self, channel):
        while not self.closed and channel.available:
            await asyncio.sleep(15)
            await self.dispatch(channel.alias, {"type": "ping"})

    async def send(self, alias, frame):
        channel = self.channels[alias]
        if not channel.available:
            raise ValueError("Browser account unavailable")
        if alias == self.main:
            self.inject(self.flow, False, json.dumps(frame).encode())
        elif channel.socket is not None:
            await channel.socket.write_message(json.dumps(frame))
        else:
            raise ValueError("Browser account unavailable")

    def discard_roster(self, alias):
        channel = self.channels[alias]
        channel.error = True
        channel.rows.clear()
        if channel.discovery is not None and not channel.discovery.done():
            channel.discovery.set_result(False)
        for exported, route in tuple(self.routes.items()):
            if route.alias == alias:
                self.routes.pop(exported)
                self.emit({"type": "peer_disconnected", "deviceId": exported})
        for call, owner in tuple(self.calls.items()):
            if owner.alias == alias:
                self.calls.pop(call)
                self.refuse(
                    {"type": "tool_call", "tool_use_id": call},
                    text="Browser disconnected before its result arrived. The outcome is unknown. Check the browser before retrying.",
                )
        for request, owner in tuple(self.external.items()):
            if owner.alias == alias:
                self.external.pop(request)
                self.emit(
                    {
                        "type": "external_message_result",
                        "requestId": request,
                        "ok": False,
                        "error": "send_failed",
                    }
                )
        self.permissions = {
            key: value
            for key, value in self.permissions.items()
            if value[0].alias != alias
        }
        self.status()

    def unavailable(self, alias):
        channel = self.channels[alias]
        channel.available = False
        if alias != self.main and channel.socket is not None:
            channel.socket.close()
        self.discard_roster(alias)

    async def discover(self):
        async def query(channel):
            if channel.alias != self.main:
                if channel.error and not channel.available and channel.task is not None:
                    await channel.task
                self.ensure_channel(channel.alias)
                await channel.ready.wait()
            if not channel.available:
                return
            channel.discovery = asyncio.get_running_loop().create_future()
            try:
                await self.send(channel.alias, {"type": "list_extensions"})
                if not await channel.discovery:
                    channel.rows.clear()
            except Exception:  # noqa: BLE001 - isolate account transport failures
                self.unavailable(channel.alias)

        try:
            async with asyncio.timeout(DISCOVERY_TIMEOUT):
                await asyncio.gather(
                    *(query(channel) for channel in self.channels.values())
                )
        except (TimeoutError, ValueError, OSError):
            pass
        finally:
            rows = []
            failures = []
            for alias, channel in self.channels.items():
                settled = channel.discovery is not None and channel.discovery.done()
                if (
                    not settled
                    or channel.discovery.cancelled()
                    or not channel.available
                ):
                    # Discovery has no request ID. Close a timed-out channel so a
                    # late old roster cannot settle the next discovery request.
                    self.unavailable(alias)
                    if alias == self.main and self.flow.killable:
                        self.flow.kill()
                    failures.append(alias)
                else:
                    rows.extend(channel.rows.values())
                channel.discovery = None
            self.status()
            if failures:
                self.emit(
                    {
                        "type": "error",
                        "error": "Chrome accounts unavailable: " + ", ".join(failures),
                    }
                )
            self.emit({"type": "extensions_list", "extensions": rows})

    def refuse(
        self,
        frame,
        *,
        text="Browser account or target unavailable. List browsers and select a connected device. No action ran.",
    ):
        if frame.get("type") == "tool_call" and isinstance(
            frame.get("tool_use_id"), str
        ):
            self.emit(
                {
                    "type": "tool_result",
                    "tool_use_id": frame["tool_use_id"],
                    "is_error": True,
                    "content": [{"type": "text", "text": text}],
                }
            )
        elif frame.get("type") == "external_message":
            self.emit(
                {
                    "type": "external_message_result",
                    "requestId": frame.get("requestId"),
                    "ok": False,
                    "error": "no_target",
                }
            )
        else:
            self.emit({"type": "error", "error": text})

    async def dispatch(self, alias, frame):
        try:
            await self.send(alias, frame)
        except Exception:  # noqa: BLE001 - isolate a failed account without exposing its payload
            self.unavailable(alias)

    def outgoing(self, frame):
        kind = frame.get("type")
        if kind in {"ping", "pong"}:
            return frame
        if kind == "list_extensions":
            if self.discovery is None or self.discovery.done():
                self.discovery = self.spawn(self.discover())
            return None
        if kind in {"pairing_request", "pairing_cancel"}:
            request = frame.get("request_id")
            if not isinstance(request, str):
                self.refuse(frame)
                return None
            if kind == "pairing_request":
                self.pairings = {request: set(self.aliases)}
            elif request not in self.pairings:
                return None
            else:
                self.pairings.pop(request)
            for alias, channel in self.channels.items():
                if channel.available:
                    self.spawn(self.dispatch(alias, frame))
            return None
        route = self.routes.get(frame.get("target_device_id"))
        if route is None or not self.channels[route.alias].available:
            self.refuse(frame)
            return None
        rewritten = {**frame, "target_device_id": route.native}
        if kind == "tool_call":
            request = frame.get("tool_use_id")
            if (
                not isinstance(request, str)
                or request in self.calls
                or len(self.calls) >= 2048
            ):
                self.refuse(frame)
                return None
            self.calls[request] = route
        elif kind == "external_message":
            request = frame.get("requestId")
            if (
                not isinstance(request, str)
                or request in self.external
                or len(self.external) >= 2048
            ):
                self.refuse(frame)
                return None
            self.external[request] = route
        elif kind == "permission_response":
            permission = self.permissions.pop(frame.get("request_id"), None)
            if permission is None or permission[0] != route:
                self.refuse(frame)
                return None
            rewritten["request_id"] = permission[1]
        elif kind != "external_config":
            self.refuse(frame)
            return None
        if route.alias == self.main:
            return rewritten
        self.spawn(self.dispatch(route.alias, rewritten))
        return None

    def incoming(self, alias, frame):
        kind = frame.get("type")
        channel = self.channels.get(alias)
        if kind in {"paired", "waiting"}:
            if channel is not None:
                channel.available = True
                channel.ready.set()
            return frame if alias == self.main else None
        if kind in {"ping", "pong"}:
            return frame if alias == self.main else None
        if channel is None:
            return None
        if kind == "extensions_list":
            if channel.discovery is None or channel.discovery.done():
                return None
            try:
                rows = frame["extensions"]
                if not isinstance(rows, list):
                    raise TypeError("Invalid browser list")
                exported = {}
                for index, row in enumerate(rows, 1):
                    device = self.route(alias, row["deviceId"])
                    name = row.get("name") or f"Browser {index}"
                    if not isinstance(name, str):
                        raise TypeError("Invalid browser name")
                    exported[device] = {
                        **row,
                        "deviceId": device,
                        "name": self.label(alias, row["deviceId"], name)[:50],
                    }
                removed = set(channel.rows) - exported.keys()
                for device in removed:
                    self.routes.pop(device, None)
                channel.rows = exported
                channel.error = False
                settled = True
            except (KeyError, ValueError, TypeError):
                settled = False
                self.discard_roster(alias)
            if channel.discovery is not None and not channel.discovery.done():
                channel.discovery.set_result(settled)
            return None
        if kind in {"peer_connected", "peer_disconnected"}:
            try:
                device = self.route(alias, frame.get("deviceId"))
            except ValueError:
                return None
            if kind == "peer_disconnected":
                self.routes.pop(device, None)
                channel.rows.pop(device, None)
            return {**frame, "deviceId": device}
        if kind == "pairing_response":
            request = frame.get("request_id")
            if alias not in self.pairings.get(request, set()):
                return None
            if frame.get("dismissed") is True:
                self.pairings.pop(request, None)
                return frame
            try:
                device = self.route(alias, frame.get("device_id"))
            except ValueError:
                return None
            self.pairings.pop(request, None)
            return {
                **frame,
                "device_id": device,
                "name": self.label(
                    alias, frame.get("device_id"), frame.get("name") or "Chrome"
                )[:50],
            }
        if kind in {"tool_result", "routing_ack", "permission_request"}:
            request = frame.get("tool_use_id")
            route = self.calls.get(request)
            if route is None or route.alias != alias:
                return None
            if kind == "tool_result":
                self.calls.pop(request)
                self.permissions = {
                    key: value
                    for key, value in self.permissions.items()
                    if value[2] != request
                }
            elif kind == "routing_ack":
                if frame.get("routed_to") not in {None, route.native}:
                    return None
                return {**frame, "routed_to": public_id(alias, route.native)}
            else:
                native = frame.get("request_id")
                if not isinstance(native, str):
                    return None
                exported = public_id(alias, "permission:" + native)
                self.permissions[exported] = (route, native, request)
                return {**frame, "request_id": exported}
            return frame
        if kind == "external_message_result":
            request = frame.get("requestId")
            route = self.external.get(request)
            if route is None or route.alias != alias:
                return None
            self.external.pop(request)
            return frame
        if kind in {"notification", "error"}:
            return frame
        return None

    def close(self):
        self.closed = True
        for task in tuple(self.tasks):
            task.cancel()
        for channel in self.channels.values():
            if channel.socket is not None:
                channel.socket.close()
        self.routes.clear()
        self.calls.clear()
        self.permissions.clear()
        self.external.clear()
        self.pairings.clear()


class ChromeBridge:
    def __init__(
        self, aliases, identity, token, inject, connect=websocket_connect, label=None
    ):
        self.aliases = aliases
        self.identity = identity
        self.token = token
        self.inject = inject
        self.connect = connect
        self.label = label
        self.sessions = {}

    def start(self, flow, main, selected=None):
        self.end(flow)
        aliases = [selected] if selected is not None else self.aliases
        session = BridgeSession(
            flow,
            main,
            aliases,
            self.identity,
            self.token,
            self.inject,
            self.connect,
            label=self.label,
        )
        self.sessions[flow.id] = session
        return session

    def message(self, flow):
        message = flow.websocket.messages[-1]
        session = self.sessions.get(flow.id)
        if session is None or message.injected:
            return False
        try:
            if not message.is_text:
                raise ValueError("Non-text Chrome bridge frame")
            frame = json.loads(message.text)
            if not isinstance(frame, dict):
                raise TypeError("Invalid Chrome bridge frame")
            output = (
                session.outgoing(frame)
                if message.from_client
                else session.incoming(session.main, frame)
            )
            if output is None:
                message.drop()
            elif output != frame:
                message.text = json.dumps(output)
        except (ValueError, TypeError, KeyError):
            message.drop()
            flow.kill()
            self.end(flow)
        return True

    def end(self, flow):
        session = self.sessions.pop(flow.id, None)
        if session is not None:
            session.close()

    def close(self):
        for session in tuple(self.sessions.values()):
            session.close()
        self.sessions.clear()
