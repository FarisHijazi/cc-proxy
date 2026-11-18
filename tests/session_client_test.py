"""SDK ownership, receipt and observation contracts, using isolated transports."""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "python"))
from cc_proxy.session_client import (
    ActionRejected,
    DeliveryUnknown,
    ObservationGap,
    SessionClient,
    SessionClientError,
)

SID = "fleet:opaque/worker?session=one"


def row(**values):
    return {"id": SID, "alive": True, "online": True, "lifecycle": "alive",
            "revision": "r1", **values}


def event(cursor="epoch:1", **values):
    return {"cursor": cursor, "source": "mod", "kind": "turn.step",
            "data": {"type": "text", "text": "original"}, **values}


def batch(events=(), cursor="epoch:0", **values):
    return {"session_id": SID, "events": list(events), "cursor": cursor,
            "reset": False, **values}


def test_private_discovery_and_dispose_only_read_and_close_local_connections():
    requests = []
    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer private-token"
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"sessions": [row()], "hosts": []})
        return httpx.Response(200, json=row())

    async def check():
        async with SessionClient("http://hq.invalid", "private-token",
                                 transport=httpx.MockTransport(handle)) as client:
            assert await client.list() == [row()]
            assert (await client.detail(SID))["id"] == SID
        await client.aclose()
        assert client.http.is_closed
        assert [r.method for r in requests] == ["GET", "GET"]
        assert requests[-1].url.raw_path == b"/api/sessions/fleet%3Aopaque%2Fworker%3Fsession%3Done"
    asyncio.run(check())


@pytest.mark.parametrize("mode", ["transport", "gateway", "inflight", "conflict", "invalid", "wrong_receipt", "redirect", "missing_receipt", "empty_receipt", "wrong_session"])
def test_uncertain_write_never_retries_and_exposes_original_request_id(mode):
    writes = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=row())
        writes.append(json.loads(request.content))
        if mode == "transport":
            raise httpx.ReadTimeout("receipt lost", request=request)
        if mode == "gateway":
            return httpx.Response(502)
        if mode == "inflight":
            return httpx.Response(409, text="Request is already in progress; do not resend with a new ID")
        if mode == "conflict":
            return httpx.Response(409, text="request ID already used with a different payload")
        if mode == "missing_receipt":
            return httpx.Response(200, json={"accepted": True})
        if mode == "empty_receipt":
            return httpx.Response(200, json={})
        if mode == "wrong_session":
            return httpx.Response(200, json={"request_id": writes[-1]["request_id"], "id": "another-session", "accepted": True})
        if mode == "wrong_receipt":
            return httpx.Response(200, json={"request_id": "unrelated", "accepted": True})
        if mode == "redirect":
            return httpx.Response(307, headers={"Location": "https://unrelated.invalid/action"})
        return httpx.Response(200, text="incomplete receipt")

    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(DeliveryUnknown) as error:
                await client.send(SID, "one original prompt")
            assert len(writes) == 1
            assert error.value.request_id == writes[0]["request_id"]
            assert writes[0]["revision"] == "r1"
    asyncio.run(check())


def test_only_confirmed_stale_revision_refreshes_and_explicit_control_revision_never_retries():
    writes = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=row(revision=f"r{len(writes) + 1}"))
        writes.append(json.loads(request.content))
        if len(writes) == 1:
            return httpx.Response(409, text="refresh the session")
        return httpx.Response(200, json={"accepted": True, "request_id": writes[-1]["request_id"]})

    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            receipt = await client.send(SID, "hello")
            assert receipt["request_id"] == writes[-1]["request_id"]
            assert [write["revision"] for write in writes] == ["r1", "r2"]
            assert len({write["request_id"] for write in writes}) == 2
            with pytest.raises(ActionRejected, match="request changed"):
                await client.respond(SID, {"request_id": "native-permission", "behavior": "allow"}, revision="r1")
            assert len(writes) == 2
    asyncio.run(check())


@pytest.mark.parametrize("detail", [row(id="a different worker"), row(alive=False), row(revision=None), []])
def test_invalid_or_unavailable_identity_prevents_any_action(detail):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=detail)
    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ActionRejected):
                await client.interrupt(SID)
            assert [r.method for r in requests] == ["GET"]
    asyncio.run(check())


def test_archived_codex_history_is_readable_but_cannot_be_controlled():
    archived = row(provider="codex", transport="codex", read_only=True, alive=False, lifecycle="ended")
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=archived)
    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            assert await client.detail(SID) == archived
            with pytest.raises(ActionRejected, match="offline or ended"):
                await client.send(SID, "must not resume")
            assert all(r.method == "GET" for r in requests)
    asyncio.run(check())


def test_action_wrappers_preserve_exact_owner_payload_and_request_identity():
    writes = []
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=row())
        writes.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True, "request_id": writes[-1]["request_id"]})
    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            await client.interrupt(SID)
            await client.respond(SID, {"request_id": "native-123", "behavior": "deny"}, revision="r1")
            await client.set_model(SID, "sonnet")
            await client.set_permission_mode(SID, "default", revision="r1")
            with pytest.raises(ValueError, match="request_id"):
                await client.action(SID, "send", request_id="old-delivery", text="bad retry")
        assert [write["action"] for write in writes] == ["interrupt", "respond", "set_model", "set_permission_mode"]
        assert writes[1]["response"] == {"request_id": "native-123", "behavior": "deny"}
        assert len({write["request_id"] for write in writes}) == 4
    asyncio.run(check())


def test_event_generator_resumes_last_cursor_preserves_payload_and_closes_without_actions():
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=batch([event("opaque:first")], "opaque:first"))
        return httpx.Response(200, json=batch([event("opaque:second", source="native", data=["original", 1])], "opaque:second"))
    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            stream = client.events(SID, wait=0)
            first, second = await anext(stream), await anext(stream)
            assert first.session_id == SID and first.data["text"] == "original"
            assert second.data == ["original", 1] and second.source == "native"
            await stream.aclose()
            assert "after" not in requests[0].url.params
            assert requests[1].url.params["after"] == first.cursor
        assert len(requests) == 2 and all(r.method == "GET" for r in requests)
    asyncio.run(check())


def test_empty_long_poll_retains_cursor_and_does_not_busy_loop():
    reads = 0
    async def check():
        nonlocal reads
        first_read = asyncio.Event()
        def handle(request):
            nonlocal reads
            reads += 1
            first_read.set()
            assert request.url.params["after"] == "opaque:last"
            return httpx.Response(200, json=batch(cursor="opaque:last"))
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            stream = client.events(SID, after="opaque:last", wait=0, poll_interval=1)
            waiting = asyncio.create_task(anext(stream))
            await first_read.wait()
            await asyncio.sleep(0.01)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            await stream.aclose()
            assert reads == 1
    asyncio.run(check())


@pytest.mark.parametrize("reason", ["expired", "reset"])
def test_cursor_gap_is_explicit_and_never_silently_advances(reason):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=batch(cursor="new:42", reset=True, gap={"reason": reason}))
    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            stream = client.events(SID, after="old:12", wait=0)
            with pytest.raises(ObservationGap) as error:
                await anext(stream)
            assert error.value.session_id == SID
            assert error.value.requested_cursor == "old:12"
            assert error.value.resume_cursor == "new:42"
            assert error.value.reason == reason
            assert len(requests) == 1
    asyncio.run(check())


@pytest.mark.parametrize("payload", [
    batch(session_id="another-session"),
    batch([event("epoch:1"), event("epoch:1")], "epoch:1"),
    batch([event("epoch:1")], "epoch:2"),
    batch([event("epoch:0")], "epoch:0"),
    batch(cursor="epoch:9"),
    batch([event("epoch:1", source="worker-commands")], "epoch:1"),
    batch([event("epoch:1", kind=None)], "epoch:1"),
    batch(cursor="", reset=True, gap={"reason": "reset"}),
])
def test_malformed_observations_fail_before_yielding(payload):
    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload))) as client:
            with pytest.raises(SessionClientError):
                await client.read_events(SID, after="epoch:0")
    asyncio.run(check())


def test_observer_read_error_requires_explicit_cursor_resume_and_never_posts():
    requests = []
    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("observation disconnected", request=request)
        return httpx.Response(200, json=batch([event("epoch:2")], "epoch:2"))
    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            stream = client.events(SID, after="epoch:1", wait=0)
            with pytest.raises(SessionClientError, match="unavailable"):
                await anext(stream)
            resumed = client.events(SID, after="epoch:1", wait=0)
            assert (await anext(resumed)).cursor == "epoch:2"
            await resumed.aclose()
            assert [r.url.params["after"] for r in requests] == ["epoch:1", "epoch:1"]
            assert all(r.method == "GET" for r in requests)
    asyncio.run(check())


def test_token_cannot_follow_redirect_or_be_sent_to_an_absolute_read_url():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.invalid/api/sessions"})
    async def check():
        async with SessionClient("http://hq.invalid", "private-token", transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(ValueError):
                await client.get("https://other.invalid/api/sessions")
            with pytest.raises(SessionClientError, match="HTTP 302"):
                await client.detail(SID)
            assert len(requests) == 1 and requests[0].url.host == "hq.invalid"
    asyncio.run(check())


@pytest.mark.parametrize("initial", ["11111111-1111-4111-8111-111111111111", "cse_placeholder"])
def test_stale_revision_retry_never_crosses_a_canonical_conversation_rotation(initial):
    writes = []
    current = "22222222-2222-4222-8222-222222222222"
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=row(transport="native", claude_session_id=initial if not writes else current,
                                                revision=f"r{len(writes) + 1}"))
        writes.append(json.loads(request.content))
        if len(writes) == 1:
            return httpx.Response(409, text="Native session changed; refresh before acting")
        return httpx.Response(200, json={"accepted": True, "request_id": writes[-1]["request_id"], "id": SID})
    async def check():
        async with SessionClient("http://hq.invalid", transport=httpx.MockTransport(handle)) as client:
            if initial.startswith("cse_"):
                await client.send(SID, "same original terminal")
                assert len(writes) == 2  # Initial qualification is not a conversation change.
            else:
                with pytest.raises(ActionRejected, match="conversation changed"):
                    await client.send(SID, "only for the first conversation")
                assert len(writes) == 1
    asyncio.run(check())
