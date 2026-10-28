"""claude.ai text-to-speech: the "play" button on an assistant message.

The web app opens one websocket per utterance (HAR 2026-10-28, entry 56)::

    wss://claude.ai/api/ws/text_to_speech/text_stream
        ?output_format=opus_48000_32&voice=mellow&tts_speed=1.20
        &client_platform=web_claude_ai&organization_uuid=<org>

The client sends ``{"type":"text_chunk","text":...}`` (one or more) then
``{"type":"close_stream"}``. The server answers with binary frames, each one
raw Opus packet (48 kHz mono, 20 ms, no container), then the text frame
``{"type": "SpeechComplete", "audio_sent_ms": N}``. Any other text frame is a
status or an error; the shapes of those are **unverified** (none was captured).

Auth: the browser sends only its ``sessionKey`` cookie (plus ``Origin``), so
cookie mode is the verified path. A Bearer OAuth token goes in
``Authorization`` — whether this route accepts it is **unverified**. Both come
from the same :meth:`RemoteControlClient._headers` every HTTP call uses, so
there is no second credential path. Only ``"mellow"`` is a known voice; any
string is passed through.

The pure-Python Ogg muxer (:func:`ogg_opus`) wraps the packets into a file any
player opens, with no native dependency.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import threading
from typing import Any, Callable, Iterable, Iterator, Optional
from urllib.parse import urlencode

from .webauth import proxy_url

# client.py imports this module (TTSMixin), so APIError is imported at call
# time; the platform value mirrors client.CLIENT_PLATFORM (a test pins it).
CLIENT_PLATFORM = "web_claude_ai"

log = logging.getLogger(__name__)

TTS_BASE_URL = "wss://claude.ai"
TTS_PATH = "/api/ws/text_to_speech/text_stream"
TTS_ORIGIN = "https://claude.ai"
OUTPUT_FORMAT = "opus_48000_32"
DEFAULT_VOICE = "mellow"
SAMPLE_RATE = 48000
# Headers worth carrying onto the upgrade; the rest of the HTTP set
# (anthropic-version, beta, Content-Type) means nothing to a websocket.
_UPGRADE_HEADERS = ("Authorization", "Cookie", "User-Agent", "X-Trusted-Device-Token")


class TTSError(RuntimeError):
    """The TTS socket sent an error frame, or closed before ``SpeechComplete``.
    ``frame`` is the offending JSON frame (or ``None`` for a bad close)."""

    def __init__(self, message: str, frame: Optional[dict] = None):
        super().__init__(message)
        self.frame = frame


# -- URL and frames (pure) ----------------------------------------------------
def tts_url(
    org_uuid: str,
    *,
    voice: str = DEFAULT_VOICE,
    speed: float = 1.0,
    output_format: str = OUTPUT_FORMAT,
    client_platform: str = CLIENT_PLATFORM,
    base_url: str = TTS_BASE_URL,
) -> str:
    """The websocket URL the web app opens. ``speed`` is sent with two
    decimals (``1.20``), as captured."""
    query = urlencode(
        {
            "output_format": output_format,
            "voice": voice,
            "tts_speed": f"{speed:.2f}",
            "client_platform": client_platform,
            "organization_uuid": org_uuid,
        }
    )
    return f"{base_url.rstrip('/')}{TTS_PATH}?{query}"


def text_chunk(text: str) -> str:
    """One ``text_chunk`` frame."""
    return json.dumps({"type": "text_chunk", "text": text})


CLOSE_STREAM = json.dumps({"type": "close_stream"})


def _chunks(text: str | Iterable[str]) -> Iterable[str]:
    return [text] if isinstance(text, str) else text


def _is_error(frame: dict) -> bool:
    kind = str(frame.get("type", ""))
    return "error" in frame or "error" in kind.lower()


# -- the websocket exchange ---------------------------------------------------
def _connect(client: Any, url: str, connect: Optional[Callable[..., Any]], open_timeout: float) -> Any:
    """Open the socket with the client's own auth headers; one credential
    renewal on a 401, like :meth:`RemoteControlClient._send`."""
    if connect is None:
        from websockets.sync.client import connect
    from websockets.exceptions import InvalidStatus

    from .client import APIError

    renewed = False
    while True:
        http = client._headers(web=False, org=False, json_body=False)
        headers = {k: v for k, v in http.items() if k in _UPGRADE_HEADERS}
        agent = headers.pop("User-Agent", None)
        # CLAUDE_AI_PROXY (e.g. a home exit node) also carries the socket past Cloudflare.
        proxy = {"proxy": proxy_url()} if proxy_url() else {}
        try:
            return connect(
                url,
                origin=TTS_ORIGIN,
                additional_headers=headers,
                user_agent_header=agent,
                open_timeout=open_timeout,
                max_size=None,
                **proxy,
            )
        except InvalidStatus as exc:
            resp = exc.response
            if resp.status_code == 401 and not renewed and client._renew_after_401():
                renewed = True
                continue
            body = (resp.body or b"").decode(errors="replace")
            raise APIError(resp.status_code, body, resp.headers.get("request-id")) from exc


def _feed(ws: Any, chunks: Iterable[str], failure: list) -> None:
    """Send every chunk then ``close_stream``; runs beside the reader so a
    lazily produced text (an LLM stream) is spoken as it arrives."""
    try:
        for chunk in chunks:
            if chunk:
                ws.send(text_chunk(chunk))
        ws.send(CLOSE_STREAM)
    except Exception as exc:  # surfaced by the reader
        failure.append(exc)


def speech_packets(
    ws: Any,
    text: str | Iterable[str],
    *,
    on_status: Optional[Callable[[dict], None]] = None,
) -> Iterator[bytes]:
    """Run the TTS protocol over an open socket: yield each raw Opus packet;
    the generator's return value is the ``SpeechComplete`` frame.

    A text frame that looks like an error (an ``error`` key or a ``type``
    containing "error") raises :class:`TTSError`; any other unknown frame
    goes to ``on_status`` (logged when absent). A close before
    ``SpeechComplete`` raises :class:`TTSError` unless it was a normal close.
    """
    from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

    failure: list = []
    sender = threading.Thread(target=_feed, args=(ws, _chunks(text), failure), daemon=True)
    sender.start()
    try:
        while True:
            try:
                frame = ws.recv()
            except ConnectionClosedOK:
                if failure:
                    raise failure[0]
                return None
            except ConnectionClosed as exc:
                raise TTSError(f"TTS socket closed before SpeechComplete: {exc}") from exc
            if isinstance(frame, (bytes, bytearray, memoryview)):
                yield bytes(frame)
                continue
            try:
                msg = json.loads(frame)
            except ValueError:
                msg = {"type": "text", "text": frame}
            if not isinstance(msg, dict):
                msg = {"type": "unknown", "value": msg}
            if msg.get("type") == "SpeechComplete":
                return msg
            if _is_error(msg):
                raise TTSError(f"TTS error frame: {msg}", msg)
            if on_status is not None:
                on_status(msg)
            else:
                log.info("tts status frame: %s", msg)
    finally:
        sender.join(timeout=1.0)


def stream_speech(
    client: Any,
    text: str | Iterable[str],
    *,
    voice: str = DEFAULT_VOICE,
    speed: float = 1.0,
    output_format: str = OUTPUT_FORMAT,
    base_url: str = TTS_BASE_URL,
    on_status: Optional[Callable[[dict], None]] = None,
    connect: Optional[Callable[..., Any]] = None,
    open_timeout: float = 10.0,
) -> Iterator[bytes]:
    """Speak ``text`` (a string, or an iterable of chunks sent as produced)
    with claude.ai's voice; yield raw Opus packets (48 kHz mono, 20 ms) as they
    arrive. Returns (as the generator's value) the ``SpeechComplete`` frame,
    ``{"type": "SpeechComplete", "audio_sent_ms": N}``.

    ``client`` is a :class:`~claude_rc.client.RemoteControlClient`: its auth
    (cookie or Bearer) and :attr:`org_uuid` are used. ``connect`` replaces
    ``websockets.sync.client.connect`` (tests)."""
    org = client.org_uuid
    if not org:
        raise ValueError("TTS needs the organization UUID (pass org_uuid= or set CLAUDE_AI_ORG_UUID)")
    url = tts_url(
        org,
        voice=voice,
        speed=speed,
        output_format=output_format,
        client_platform=getattr(client, "client_platform", CLIENT_PLATFORM),
        base_url=base_url,
    )
    with _connect(client, url, connect, open_timeout) as ws:
        return (yield from speech_packets(ws, text, on_status=on_status))


def collect(packets: Iterator[bytes]) -> tuple[list[bytes], Optional[dict]]:
    """Drain a :func:`stream_speech` generator: ``(packets, SpeechComplete)``."""
    out: list[bytes] = []
    while True:
        try:
            out.append(next(packets))
        except StopIteration as stop:
            return out, stop.value


def synthesize(client: Any, text: str | Iterable[str], **kw: Any) -> bytes:
    """:func:`stream_speech` muxed into one Ogg Opus file (``audio/ogg``)."""
    packets, _ = collect(stream_speech(client, text, **kw))
    return ogg_opus(packets)


def save_speech(client: Any, path: str | os.PathLike, text: str | Iterable[str], **kw: Any) -> int:
    """:func:`synthesize` written to ``path`` (``.ogg`` / ``.opus``); returns
    the byte count."""
    data = synthesize(client, text, **kw)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)


class TTSMixin:
    """TTS methods for :class:`~claude_rc.client.RemoteControlClient` (thin
    wrappers over the module functions, which take the client explicitly)."""

    def stream_speech(self, text: str | Iterable[str], **kw: Any) -> Iterator[bytes]:
        """See :func:`claude_rc.tts.stream_speech`."""
        return stream_speech(self, text, **kw)

    def synthesize_speech(self, text: str | Iterable[str], **kw: Any) -> bytes:
        """See :func:`claude_rc.tts.synthesize`."""
        return synthesize(self, text, **kw)

    def save_speech(self, path: str | os.PathLike, text: str | Iterable[str], **kw: Any) -> int:
        """See :func:`claude_rc.tts.save_speech`."""
        return save_speech(self, path, text, **kw)


# -- Ogg Opus (RFC 7845), pure Python -----------------------------------------
def _crc_table() -> tuple[int, ...]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else r << 1
        table.append(r & 0xFFFFFFFF)
    return tuple(table)


_CRC = _crc_table()


def ogg_crc(data: bytes) -> int:
    """The Ogg page checksum (CRC-32, poly 0x04C11DB7, no reflection)."""
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC[(c >> 24) ^ b]
    return c


def ogg_page(packet: bytes, granule: int, seq: int, serial: int, flags: int) -> bytes:
    """One Ogg page carrying exactly one packet (flags: 2 = BOS, 4 = EOS)."""
    if len(packet) >= 255 * 255:
        raise ValueError(f"packet of {len(packet)} bytes does not fit one Ogg page")
    lacing = bytes([255] * (len(packet) // 255) + [len(packet) % 255])
    head = struct.pack("<4sBBqIIIB", b"OggS", 0, flags, granule, serial, seq, 0, len(lacing)) + lacing
    crc = ogg_crc(head + packet)
    return head[:22] + struct.pack("<I", crc) + head[26:] + packet


def opus_samples(packet: bytes) -> int:
    """48 kHz samples in one Opus packet, from its TOC byte (RFC 6716 §3.1)."""
    toc = packet[0]
    config = toc >> 3
    if config < 12:  # SILK: 10/20/40/60 ms
        size = (480, 960, 1920, 2880)[config & 3]
    elif config < 16:  # hybrid: 10/20 ms
        size = (480, 960)[config & 1]
    else:  # CELT: 2.5/5/10/20 ms
        size = (120, 240, 480, 960)[config & 3]
    count = toc & 3
    frames = 1 if count == 0 else 2 if count in (1, 2) else packet[1] & 0x3F
    return size * frames


def ogg_opus(
    packets: Iterable[bytes],
    *,
    channels: int = 1,
    pre_skip: int = 312,
    vendor: bytes = b"claude-rc",
    serial: int = 0x43435052,
) -> bytes:
    """Raw Opus packets -> an Ogg Opus file: ``OpusHead``, ``OpusTags``, then
    one page per packet, granule = cumulative 48 kHz samples."""
    packets = list(packets)
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, channels, pre_skip, SAMPLE_RATE, 0, 0)
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)
    out = [ogg_page(head, 0, 0, serial, 2), ogg_page(tags, 0, 1, serial, 0)]
    granule = 0
    last = len(packets) - 1
    for i, packet in enumerate(packets):
        granule += opus_samples(packet)
        out.append(ogg_page(packet, granule, i + 2, serial, 4 if i == last else 0))
    return b"".join(out)


def ogg_last_granule(data: bytes) -> int:
    """The granule position of an Ogg stream's last page (walks the pages, so
    an ``OggS`` inside packet data cannot fool it)."""
    at, granule = 0, None
    while at + 27 <= len(data):
        if data[at : at + 4] != b"OggS":
            raise ValueError(f"no Ogg page at byte {at}")
        granule = struct.unpack_from("<q", data, at + 6)[0]
        segments = data[at + 27 : at + 27 + data[at + 26]]
        at += 27 + len(segments) + sum(segments)
    if granule is None:
        raise ValueError("not an Ogg stream")
    return granule


def ogg_duration_ms(data: bytes) -> float:
    """Audio length of an Ogg Opus file from its last granule (pre-skip not
    subtracted: it equals the server's ``audio_sent_ms``)."""
    return ogg_last_granule(data) * 1000 / SAMPLE_RATE
