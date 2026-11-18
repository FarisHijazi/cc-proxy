"""Async clients for existing HQ sessions; closing this client only detaches.

HQ owns session identity, current revisions, receipts, and all mutations. This
module never launches a worker, consumes a worker command stream, or retries an
uncertain action. ACP and MCP are optional adapters, not SDK dependencies.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import quote

import httpx


class SessionClientError(Exception):
    """A read, protocol, or configuration error; no action is implied."""


class ActionRejected(SessionClientError):
    """The action was not sent, or the owner definitively rejected it."""


class DeliveryUnknown(SessionClientError):
    """A write may have reached the owner. Inspect it; do not resend."""

    def __init__(self, message: str, request_id: str):
        super().__init__(message)
        self.request_id = request_id


class ObservationGap(SessionClientError):
    """The requested cursor cannot be resumed without losing observations."""

    def __init__(self, session_id: str, requested_cursor: str | None,
                 resume_cursor: str, reason: str):
        super().__init__(f"Session observations have a {reason} gap; refresh the session before resuming.")
        self.session_id = session_id
        self.requested_cursor = requested_cursor
        self.resume_cursor = resume_cursor
        self.reason = reason


@dataclass(frozen=True)
class SessionEvent:
    session_id: str
    cursor: str
    source: str
    kind: str
    data: Any
    id: str | None = None


@dataclass(frozen=True)
class EventBatch:
    session_id: str
    events: tuple[SessionEvent, ...]
    cursor: str


def available(row: dict[str, Any]) -> bool:
    """The existing fleet control-availability policy, shared with ACP."""
    return (row.get("alive") is True and row.get("online") is not False
            and row.get("read_only") is not True
            and row.get("lifecycle") not in {"ended", "unreachable", "unknown"})


def _session_path(session_id: str) -> str:
    if not isinstance(session_id, str) or not session_id or session_id in {".", ".."}:
        raise ValueError("session_id must be the nonempty opaque ID returned by HQ")
    return f"/api/sessions/{quote(session_id, safe='')}"


def _native_conversation(row: dict[str, Any]) -> str | None:
    value = row.get("claude_session_id")
    if row.get("transport") != "native" or not isinstance(value, str):
        return None
    try:
        return value if str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


class SessionClient:
    """Use an existing authenticated HQ, optionally discovering through MCP.

    ``token`` is the existing private HQ bearer credential. ``mcp_token`` belongs
    only to ``mcp_url``; credentials are never copied between those origins.
    Importing or creating this client performs no network IO. There are no
    background tasks. Each events() iterator observes only its selected session.
    """

    def __init__(self, url: str, token: str = "", *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 mcp_url: str | None = None, mcp_token: str | None = None,
                 timeout: float = 180):
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            transport=transport, follow_redirects=False,
        )
        self.mcp_url = mcp_url
        self.mcp_token = mcp_token

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release local connections. Never stop, archive, or signal a worker."""
        await self.http.aclose()

    async def list(self) -> list[dict[str, Any]]:
        """Return existing sessions, from compact MCP or a private HQ snapshot."""
        if self.mcp_url:
            rows = await self._list_mcp()
        else:
            data = await self.get("/api/sessions")
            rows = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not all(
                isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
                for row in rows):
            raise SessionClientError("Fleet session discovery returned an invalid list.")
        return rows

    async def _list_mcp(self) -> Any:
        # The HTTP-only SDK does not require the MCP or ACP packages.
        try:
            import httpx2
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise SessionClientError("MCP discovery requires the cc-proxy[sessions-mcp] extra.") from exc
        try:
            async with (
                httpx2.AsyncClient(headers={"Authorization": f"Bearer {self.mcp_token}"}
                                  if self.mcp_token else {}, timeout=180) as http,
                streamable_http_client(self.mcp_url, http_client=http) as (read, write, *_),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                result = await client.call_tool("list_sessions", {})
                if result.is_error:
                    raise SessionClientError("Fleet session discovery failed.")
                data = result.structured_content
                return data["result"] if data is not None else json.loads(result.content[0].text)
        except SessionClientError:
            raise
        except Exception as exc:
            raise SessionClientError("Fleet session discovery failed.") from exc

    async def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """Read an HQ-relative API path; absolute URLs cannot receive its token."""
        if not path.startswith("/api/") or path.startswith("//") or "\\" in path:
            raise ValueError("HQ reads must use an /api/ path")
        try:
            response = await self.http.get(path, params=params)
        except httpx.TransportError as exc:
            raise SessionClientError("Fleet HQ is unavailable; the existing session was left running.") from exc
        if response.status_code != 200:
            raise SessionClientError(f"Fleet HQ read failed (HTTP {response.status_code}).")
        try:
            return response.json()
        except ValueError as exc:
            raise SessionClientError("Fleet HQ returned invalid JSON.") from exc

    async def detail(self, session_id: str) -> dict[str, Any]:
        row = await self.get(_session_path(session_id))
        if not isinstance(row, dict) or row.get("id") != session_id:
            raise SessionClientError("Fleet HQ returned a different session identity.")
        archived = (row.get("provider") == "codex" and row.get("transport") == "codex"
                    and row.get("read_only") is True and row.get("alive") is False
                    and row.get("online") is True and row.get("lifecycle") == "ended")
        if not (available(row) or archived) or not row.get("revision"):
            raise SessionClientError("This fleet session is offline or ended; it cannot be controlled.")
        return row

    async def action(self, session_id: str, action: str, *, revision: str | None = None,
                     **fields: Any) -> dict[str, Any]:
        """Send through HQ's existing action owner, returning its original receipt.

        Only a definitive stale-revision rejection can trigger a new attempt.
        Transport failures, conflicting receipts, redirects and 5xx responses
        are uncertain writes. They never trigger an automatic retry.
        """
        path = _session_path(session_id)
        if "request_id" in fields:
            raise ValueError("request_id is owned by SessionClient; do not replay commands")
        conversation = None
        for attempt in range(3):
            request_id = uuid.uuid4().hex
            try:
                row = await self.detail(session_id)
            except (SessionClientError, httpx.TransportError, ValueError) as exc:
                raise ActionRejected("Fleet action was not sent because its current state could not be verified.") from exc
            if not available(row):
                raise ActionRejected("This fleet session is offline or ended; it cannot be controlled.")
            observed_conversation = _native_conversation(row)
            if conversation is not None and observed_conversation != conversation:
                raise ActionRejected("The session conversation changed. Read the session again before acting.")
            conversation = observed_conversation
            if revision is not None and revision != row["revision"]:
                raise ActionRejected("The request changed. Read the session again before answering.")
            body = {"action": action, "revision": revision or row["revision"],
                    "request_id": request_id, **fields}
            try:
                response = await self.http.post(f"{path}/action", json=body)
            except httpx.TransportError as exc:
                raise DeliveryUnknown(f"Delivery is unknown; do not resend. request_id={request_id}", request_id) from exc
            if response.status_code == 409 and any(phrase in response.text.lower() for phrase in (
                    "already in progress", "already used with a different payload")):
                raise DeliveryUnknown(f"Delivery is unknown; conflicting action receipt. Do not resend. request_id={request_id}", request_id)
            if (response.status_code == 409 and "refresh" in response.text.lower()
                    and revision is None and attempt < 2):
                continue
            if response.status_code in {400, 401, 403, 404, 409, 422}:
                raise ActionRejected(f"Fleet action rejected (HTTP {response.status_code}); request_id={request_id}")
            if response.status_code >= 400 or 300 <= response.status_code < 400:
                raise DeliveryUnknown(f"Delivery is unknown (HTTP {response.status_code}); do not resend. request_id={request_id}", request_id)
            try:
                receipt = response.json()
                if (not isinstance(receipt, dict) or receipt.get("request_id") != request_id
                        or ("id" in receipt and receipt["id"] != session_id)):
                    raise ValueError("invalid receipt identity")
                return receipt
            except ValueError as exc:
                raise DeliveryUnknown(f"Delivery is unknown; invalid action receipt. Do not resend. request_id={request_id}", request_id) from exc
        raise AssertionError("unreachable stale-revision retry state")

    async def send(self, session_id: str, text: str, *, revision: str | None = None) -> dict[str, Any]:
        return await self.action(session_id, "send", revision=revision, text=text)

    async def interrupt(self, session_id: str, *, revision: str | None = None) -> dict[str, Any]:
        return await self.action(session_id, "interrupt", revision=revision)

    async def respond(self, session_id: str, response: dict[str, Any], *, revision: str) -> dict[str, Any]:
        """Answer the exact native request from a detail snapshot at its revision."""
        if not revision:
            raise ValueError("respond requires the revision of the request you inspected")
        return await self.action(session_id, "respond", revision=revision, response=response)

    async def set_model(self, session_id: str, model: str, *, revision: str | None = None) -> dict[str, Any]:
        return await self.action(session_id, "set_model", revision=revision, text=model)

    async def set_permission_mode(self, session_id: str, mode: str, *, revision: str) -> dict[str, Any]:
        return await self.action(session_id, "set_permission_mode", revision=revision, text=mode)

    async def read_events(self, session_id: str, *, after: str | None = None,
                          wait: float = 0) -> EventBatch:
        """Read retained observations; this is not the CLI worker command stream.

        No cursor means the retained tail, not complete transcript history.
        A reset/expired cursor raises ObservationGap with the explicit new cursor;
        the caller chooses whether to refresh detail and resume from that point.
        """
        if not math.isfinite(wait) or not 0 <= wait <= 20:
            raise ValueError("wait must be between 0 and 20 seconds")
        if after is not None and (not isinstance(after, str) or not after):
            raise ValueError("after must be a nonempty opaque cursor")
        params = {"wait": wait}
        if after is not None:
            params["after"] = after
        data = await self.get(f"{_session_path(session_id)}/events", params=params)
        if not isinstance(data, dict) or data.get("session_id") != session_id:
            raise SessionClientError("Fleet HQ returned a different observation session identity.")
        cursor = data.get("cursor")
        if not isinstance(cursor, str) or not cursor:
            raise SessionClientError("Fleet HQ returned an invalid observation cursor.")
        if data.get("reset") is True:
            gap = data.get("gap")
            reason = gap.get("reason") if isinstance(gap, dict) else None
            if reason not in {"expired", "reset"}:
                raise SessionClientError("Fleet HQ returned an invalid observation gap.")
            raise ObservationGap(session_id, after, cursor, reason)
        if data.get("reset") is not False or data.get("gap") is not None or not isinstance(data.get("events"), list):
            raise SessionClientError("Fleet HQ returned an invalid observation batch.")
        events = []
        seen = {after} if after is not None else set()
        for event in data["events"]:
            if (not isinstance(event, dict) or not isinstance(event.get("cursor"), str)
                    or not event["cursor"] or event["cursor"] in seen
                    or event.get("source") not in {"native", "mod"}
                    or not isinstance(event.get("kind"), str) or not event["kind"]
                    or "data" not in event
                    or (event.get("id") is not None and not isinstance(event["id"], str))):
                raise SessionClientError("Fleet HQ returned an invalid observation event.")
            seen.add(event["cursor"])
            events.append(SessionEvent(session_id, event["cursor"], event["source"],
                                       event["kind"], event["data"], event.get("id")))
        if ((events and events[-1].cursor != cursor)
                or (not events and after is not None and after != cursor)):
            raise SessionClientError("Fleet HQ returned a discontinuous observation cursor.")
        return EventBatch(session_id, tuple(events), cursor)

    async def events(self, session_id: str, *, after: str | None = None,
                     wait: float = 20, poll_interval: float = 1) -> AsyncIterator[SessionEvent]:
        """Yield observations, resuming each read at the last received cursor.

        Read failures propagate. Restart explicitly with your last consumed
        event.cursor; no command is replayed. Cancellation/closing the generator
        stops reads only. poll_interval bounds immediate-empty server responses.
        """
        if not math.isfinite(poll_interval) or not 0.05 <= poll_interval <= 60:
            raise ValueError("poll_interval must be between 0.05 and 60 seconds")
        cursor = after
        while True:
            started = time.monotonic()
            batch = await self.read_events(session_id, after=cursor, wait=wait)
            for event in batch.events:
                yield event
            cursor = batch.cursor
            if not batch.events:
                await asyncio.sleep(max(0, poll_interval - (time.monotonic() - started)))
