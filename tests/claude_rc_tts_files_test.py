"""TTS websocket + Ogg muxing, and file uploads — offline (MockTransport and an
in-process websockets server)."""
import base64
import functools
import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from websockets.sync.client import connect as ws_connect
from websockets.sync.server import serve

from claude_rc import client as client_mod
from claude_rc import files as files_mod
from claude_rc import tts
from claude_rc.client import APIError, RemoteControlClient
from claude_rc.files import attachment_ref, user_message_with_files

FIXTURE = Path(__file__).parent / "fixtures" / "claude_rc_tts_packets.json"
ORG = "00000000-0000-4000-8000-000000000001"

AUTH_ENV = (
    "CLAUDE_AI_ACCESS_TOKEN",
    "CLAUDE_RC_ACCESS_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_AI_SESSION_KEY",
    "CLAUDE_RC_CSWAP_SLOT",
    "CLAUDE_AI_ORG_UUID",
    "CLAUDE_RC_ORG_UUID",
    "CLAUDE_TRUSTED_DEVICE_TOKEN",
)


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    for name in AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)


def _client(handler=None, **kw) -> RemoteControlClient:
    kw.setdefault("org_uuid", ORG)
    if "session_key" not in kw:
        kw.setdefault("token", "tok")
    handler = handler or (lambda req: httpx.Response(500))
    return RemoteControlClient(transport=httpx.MockTransport(handler), **kw)


def _captured():
    fx = json.loads(FIXTURE.read_text())
    return [base64.b64decode(p) for p in fx["packets"]], fx["audio_sent_ms"]


# --- Ogg muxing on the real captured packets ------------------------------------
def test_captured_packets_mux_to_the_reported_duration(tmp_path):
    packets, sent_ms = _captured()
    assert len(packets) == 436
    assert {p[0] for p in packets} <= {0x68, 0x78}  # hybrid FB 20 ms
    ogg = tts.ogg_opus(packets)
    assert ogg[:4] == b"OggS" and b"OpusHead" in ogg[:64]
    assert tts.ogg_last_granule(ogg) == 436 * 960
    assert tts.ogg_duration_ms(ogg) == sent_ms == 8720
    if shutil.which("ffprobe"):
        path = tmp_path / "speech.ogg"
        path.write_bytes(ogg)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels",
             "-of", "json", str(path)],
            capture_output=True, text=True, check=True,
        )
        stream = json.loads(out.stdout)["streams"][0]
        assert stream["codec_name"] == "opus" and stream["channels"] == 1
        decode = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                                capture_output=True, text=True)
        assert decode.returncode == 0 and not decode.stderr.strip()


def test_ogg_pages_checksum_and_toc_math():
    page = tts.ogg_page(b"x" * 300, 960, 2, 7, 0)
    stored = int.from_bytes(page[22:26], "little")
    assert tts.ogg_crc(page[:22] + b"\0\0\0\0" + page[26:]) == stored
    assert page[26] == 2 and page[27:29] == bytes([255, 45])  # lacing for 300 bytes
    assert tts.opus_samples(bytes([0x68])) == 960   # hybrid FB 20 ms
    assert tts.opus_samples(bytes([0xFC])) == 960   # CELT 20 ms
    assert tts.opus_samples(bytes([0xFD])) == 1920  # CELT 20 ms, two frames
    assert tts.opus_samples(bytes([0x03, 0x05])) == 2400  # SILK 10 ms x 5
    with pytest.raises(ValueError):
        tts.ogg_page(b"x" * (255 * 255), 0, 0, 0, 0)


def test_tts_url_matches_the_web_app():
    url = tts.tts_url(ORG, voice="mellow", speed=1.2)
    assert url == (
        "wss://claude.ai/api/ws/text_to_speech/text_stream?output_format=opus_48000_32"
        f"&voice=mellow&tts_speed=1.20&client_platform=web_claude_ai&organization_uuid={ORG}"
    )


# --- the websocket protocol against a fake in-process server --------------------
class FakeTTS:
    """websockets.sync.server playing claude.ai's TTS endpoint."""

    def __init__(self, packets, *, finish=None, status_frames=(), reject=None):
        self.packets = packets
        self.finish = finish if finish is not None else {"type": "SpeechComplete", "audio_sent_ms": 8720}
        self.status_frames = status_frames
        self.reject = reject  # list of statuses to answer the upgrade with, in turn
        self.received = []
        self.requests = []
        self.server = serve(self.handler, "127.0.0.1", 0, process_request=self.process_request)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def process_request(self, conn, request):
        self.requests.append(request)
        if self.reject:
            return conn.respond(self.reject.pop(0), "nope\n")
        return None

    def handler(self, ws):
        for frame in ws:
            msg = json.loads(frame)
            self.received.append(msg)
            if msg["type"] == "close_stream":
                break
        for s in self.status_frames:
            ws.send(json.dumps(s))
        for p in self.packets:
            ws.send(p)
        if self.finish:
            ws.send(json.dumps(self.finish))

    @property
    def base(self):
        return f"ws://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()


CONNECT = functools.partial(ws_connect, proxy=None)


@pytest.fixture
def fake():
    made = []

    def make(packets, **kw):
        made.append(FakeTTS(packets, **kw))
        return made[-1]

    yield make
    for f in made:
        f.close()


def test_stream_speech_protocol_and_cookie_auth(fake):
    packets, _ = _captured()
    srv = fake(packets[:20])
    rc = _client(session_key="sk-test")
    gen = tts.stream_speech(rc, ["Hi, ", "there."], voice="mellow", speed=1.2,
                            base_url=srv.base, connect=CONNECT)
    got, done = tts.collect(gen)
    assert got == packets[:20]
    assert done == {"type": "SpeechComplete", "audio_sent_ms": 8720}
    assert srv.received == [{"type": "text_chunk", "text": "Hi, "}, {"type": "text_chunk", "text": "there."},
                            {"type": "close_stream"}]
    req = srv.requests[0]
    assert req.path.startswith("/api/ws/text_to_speech/text_stream?output_format=opus_48000_32&voice=mellow")
    assert f"organization_uuid={ORG}" in req.path and "tts_speed=1.20" in req.path
    assert req.headers["Cookie"] == "sessionKey=sk-test"
    assert req.headers["Origin"] == "https://claude.ai"
    assert "Authorization" not in req.headers
    assert req.headers["User-Agent"].startswith("claude-rc-api-python/")


def test_synthesize_and_save_with_bearer(fake, tmp_path):
    packets, _ = _captured()
    srv = fake(packets)
    rc = _client(token="bearer-tok")
    out = tmp_path / "s.ogg"
    n = tts.save_speech(rc, out, "hello", base_url=srv.base, connect=CONNECT)
    assert n == out.stat().st_size
    assert tts.ogg_duration_ms(out.read_bytes()) == 8720
    assert srv.requests[0].headers["Authorization"] == "Bearer bearer-tok"
    assert srv.received[0] == {"type": "text_chunk", "text": "hello"}


def test_error_frame_raises_and_status_frames_are_surfaced(fake):
    srv = fake([b"\x68\x00"], status_frames=[{"type": "queued"}], finish={"type": "error", "message": "quota"})
    seen = []
    with pytest.raises(tts.TTSError) as err:
        tts.collect(tts.stream_speech(_client(), "x", base_url=srv.base, connect=CONNECT, on_status=seen.append))
    assert err.value.frame == {"type": "error", "message": "quota"}
    assert seen == [{"type": "queued"}]


def test_upgrade_rejection_becomes_apierror(fake):
    srv = fake([], reject=[403])
    with pytest.raises(APIError) as err:
        tts.collect(tts.stream_speech(_client(), "x", base_url=srv.base, connect=CONNECT))
    assert err.value.status == 403


def test_missing_org_is_a_clear_error():
    rc = _client(org_uuid=None, handler=lambda req: httpx.Response(404))  # profile lookup fails
    with pytest.raises(ValueError, match="organization UUID"):
        next(tts.stream_speech(rc, "x"))


def test_tts_mixin_delegates(fake):
    from claude_rc import client as client_mod

    assert issubclass(RemoteControlClient, tts.TTSMixin)
    assert tts.CLIENT_PLATFORM == client_mod.CLIENT_PLATFORM
    srv = fake([b"\x68\x00"])
    rc = RemoteControlClient(token="tok", org_uuid=ORG, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    ogg = rc.synthesize_speech("hi", base_url=srv.base, connect=CONNECT)
    assert tts.ogg_duration_ms(ogg) == 20


# --- files ------------------------------------------------------------------------
def _recorder(reply_status=201, reply=None):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(reply_status, json=reply if reply is not None else {})

    return seen, handler


def _multipart(req):
    ctype = req.headers["content-type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    return req.content


def test_upload_attachment_multipart_and_reference(tmp_path):
    seen, handler = _recorder(201, {"file_uuid": "fu-1", "file_kind": "image"})
    rc = _client(handler)
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG-data")
    up = rc.upload_attachment(img, lane="prompt")
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/api/oauth/file_upload"
    body = _multipart(req)
    assert b'name="file"; filename="shot.png"' in body and b"Content-Type: image/png" in body
    assert b"\x89PNG-data" in body
    assert req.headers["x-claude-code-upload-lane"] == "prompt"
    assert req.headers["authorization"] == "Bearer tok"
    assert "anthropic-client-feature" not in req.headers and "x-organization-uuid" not in req.headers
    assert up["file_uuid"] == "fu-1" and up["file_name"] == "shot.png" and up["is_image"] is True
    assert up["sha256"] == hashlib.sha256(b"\x89PNG-data").hexdigest() and up["file_size"] == 9


def test_attachment_ref_and_message_shape():
    ref = attachment_ref({"file_uuid": "u"}, "a.txt", data=b"abc", mime="text/plain")
    assert ref == {"file_uuid": "u", "file_name": "a.txt", "is_image": False,
                   "sha256": hashlib.sha256(b"abc").hexdigest(), "file_size": 3}
    assert attachment_ref("u2", "b.bin") == {"file_uuid": "u2", "file_name": "b.bin"}
    ev = user_message_with_files("look", [ref], message_uuid="m-1")
    assert ev["type"] == "user" and ev["uuid"] == "m-1"
    assert ev["message"]["content"] == [{"type": "text", "text": "look"}]
    assert ev["file_attachments"] == [ref]


def test_send_message_with_files_uploads_then_sends():
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path == "/api/oauth/file_upload":
            return httpx.Response(201, json={"file_uuid": "fu-9"})
        return httpx.Response(200, json={"results": [{"sequence_num": 3}]})

    rc = _client(handler)
    rc.send_message_with_files("cse_1", "see attached", [b"hello"])
    assert [r.url.path for r in seen] == ["/api/oauth/file_upload", "/v1/code/sessions/cse_1/events"]
    payload = json.loads(seen[1].content)["events"][0]["payload"]
    assert payload["file_attachments"] == [{"file_uuid": "fu-9", "file_name": "upload.bin", "is_image": False,
                                            "sha256": hashlib.sha256(b"hello").hexdigest(), "file_size": 5}]
    assert payload["message"]["content"][0]["text"] == "see attached"


def test_upload_file_files_api_multipart():
    seen, handler = _recorder(200, {"id": "file_abc"})
    rc = _client(handler)
    out = rc.upload_file(b"bundle", "_source_seed.bundle", "application/octet-stream", expires_in_seconds=3600)
    assert out == {"id": "file_abc"}
    req = seen[0]
    assert req.url.path == "/v1/files"
    assert req.headers["anthropic-beta"] == "files-api-2025-04-14,oauth-2025-04-20"
    body = _multipart(req)
    assert b'name="purpose"\r\n\r\nuser_data' in body
    assert b'name="expires_in_seconds"\r\n\r\n3600' in body
    assert b'filename="_source_seed.bundle"' in body and b"bundle" in body


def test_files_betas_match_client():
    assert files_mod.FILES_BETAS == client_mod.FILES_BETAS


def test_delete_and_download_file():
    seen = []

    def handler(req):
        seen.append(req)
        if req.method == "DELETE":
            return httpx.Response(204 if req.url.path.endswith("file_a") else 404)
        return httpx.Response(200, content=b"bytes!")

    rc = _client(handler)
    assert rc.delete_file("file_a") is True
    assert rc.delete_file("file_gone") is False
    assert rc.download_file("file_a") == b"bytes!"
    assert rc.download_attachment("fu-1") == b"bytes!"
    assert seen[0].headers["anthropic-beta"] == "files-api-2025-04-14,oauth-2025-04-20"
    assert seen[3].url.path == "/api/oauth/files/fu-1/content"


def test_upload_without_file_uuid_is_an_error():
    _, handler = _recorder(201, {"weird": True})
    with pytest.raises(ValueError):
        _client(handler).upload_attachment(b"x")
