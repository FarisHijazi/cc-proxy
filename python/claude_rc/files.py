"""File uploads for sessions.

Two upload routes exist, for different jobs (spec surface.md §4.2/§4.4,
sessions.md §4/§5):

* **Chat attachments** — ``POST /api/oauth/file_upload``, multipart field
  ``file`` (real content type), optional ``x-claude-code-upload-lane`` header
  -> **201** ``{file_uuid, file_kind?}``. The message references it in
  ``user.file_attachments: [{file_uuid, file_name, is_image?, sha256?,
  file_size?}]``; the worker fetches ``GET /api/oauth/files/{uuid}/content``
  and emits ``system/file_attachments_missing`` if it cannot. A 413 carries
  ``error.details.max_bytes``. This is what :meth:`send_message_with_files`
  uses. The host for OAuth clients is assumed to be :attr:`base_url`
  (``api.anthropic.com``) — **unverified** (sessions.md §10 open question).
* **Files API** — ``POST /v1/files``, multipart ``file`` + ``purpose=user_data``
  [+ ``expires_in_seconds``], ``anthropic-beta: files-api-2025-04-14,
  oauth-2025-04-20`` -> ``{id: "file_…"}``; ``DELETE /v1/files/{id}``,
  ``GET /v1/files/{id}/content``. The CLI uses it for teleport seed bundles
  (``config.seed_bundle_file_id``), not for chat attachments.

Multipart bodies go through ``files=`` as bytes so a retry can resend them.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from typing import Any, Iterable, Mapping

from .events import cli_user_message

# Equal to ``client.FILES_BETAS``; client.py imports this module, so it is
# restated here rather than imported (a test pins the two together).
FILES_BETAS = ("files-api-2025-04-14", "oauth-2025-04-20")

FILE_UPLOAD_PATH = "/api/oauth/file_upload"
FILES_PATH = "/v1/files"
UPLOAD_LANE_HEADER = "x-claude-code-upload-lane"
SEED_BUNDLE_PURPOSE = "user_data"


def read_upload(
    file: str | os.PathLike | bytes, filename: str | None = None, mime: str | None = None
) -> tuple[bytes, str, str]:
    """``(data, filename, mime)`` for a path or raw bytes. The name defaults to
    the path's basename (``"upload.bin"`` for bytes), the type to a guess from
    the name (``application/octet-stream`` if none)."""
    if isinstance(file, (bytes, bytearray, memoryview)):
        data = bytes(file)
        name = filename or "upload.bin"
    else:
        with open(file, "rb") as f:
            data = f.read()
        name = filename or os.path.basename(os.fspath(file))
    kind = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return data, name, kind


def attachment_ref(
    upload: Mapping[str, Any] | str,
    file_name: str,
    *,
    data: bytes | None = None,
    mime: str | None = None,
    is_image: bool | None = None,
) -> dict:
    """One ``file_attachments`` entry for a user message:
    ``{file_uuid, file_name, is_image?, sha256?, file_size?}``.

    ``upload`` is the ``/api/oauth/file_upload`` reply (or its ``file_uuid``).
    With ``data`` the ``sha256`` and ``file_size`` are filled in; ``is_image``
    defaults from the reply's ``file_kind`` or ``mime``."""
    if isinstance(upload, str):
        file_uuid, kind = upload, None
    else:
        file_uuid, kind = upload["file_uuid"], upload.get("file_kind")
    ref: dict[str, Any] = {"file_uuid": file_uuid, "file_name": file_name}
    if is_image is None:
        if kind is not None:
            is_image = kind == "image"
        elif mime is not None:
            is_image = mime.startswith("image/")
    if is_image is not None:
        ref["is_image"] = is_image
    if data is not None:
        ref["sha256"] = hashlib.sha256(data).hexdigest()
        ref["file_size"] = len(data)
    return ref


def user_message_with_files(text: str, attachments: Iterable[Mapping[str, Any]], **kw: Any) -> dict:
    """A stream-json ``user`` event carrying ``file_attachments`` (top level of
    the event, beside ``message``; sessions.md §5)."""
    return {**cli_user_message(text, **kw), "file_attachments": [dict(a) for a in attachments]}


class FilesMixin:
    """File-upload methods for :class:`~claude_rc.client.RemoteControlClient`.

    All HTTP goes through ``self._request`` / ``self._send``.
    """

    def upload_attachment(
        self,
        file: str | os.PathLike | bytes,
        filename: str | None = None,
        mime: str | None = None,
        *,
        lane: str | None = None,
    ) -> dict:
        """``POST /api/oauth/file_upload`` — upload a chat attachment. Returns
        the reply ``{file_uuid, file_kind?}`` plus the local ``file_name``,
        ``sha256``, ``file_size`` and ``is_image`` (when known), ready for
        :func:`attachment_ref`. Sent like the CLI: bare OAuth headers (no CCR
        beta / org), ``lane`` as ``x-claude-code-upload-lane``."""
        data, name, kind = read_upload(file, filename, mime)
        reply = self._request(
            "POST",
            FILE_UPLOAD_PATH,
            files={"file": (name, data, kind)},
            headers={UPLOAD_LANE_HEADER: lane} if lane else None,
            web=False,
            org=False,
            accept=(200, 201),
        )
        if not isinstance(reply, dict) or "file_uuid" not in reply:
            raise ValueError(f"file_upload replied without file_uuid: {reply!r}")
        return {**reply, **attachment_ref(reply, name, data=data, mime=kind)}

    def download_attachment(self, file_uuid: str) -> bytes:
        """``GET /api/oauth/files/{uuid}/content`` — an attachment's bytes."""
        return self._send("GET", f"/api/oauth/files/{file_uuid}/content", web=False, org=False).content

    def send_message_with_files(
        self,
        session_id: str,
        text: str,
        files: Iterable[str | os.PathLike | bytes | Mapping[str, Any]],
        *,
        message_uuid: str | None = None,
    ) -> dict:
        """Upload each of ``files`` (paths / bytes; a mapping is taken as an
        already built :func:`attachment_ref`) via :meth:`upload_attachment`, then
        send ``text`` with them as ``file_attachments``. Returns the events ack."""
        refs = []
        for f in files:
            if isinstance(f, Mapping):
                refs.append(dict(f))
                continue
            up = self.upload_attachment(f)
            refs.append({k: up[k] for k in ("file_uuid", "file_name", "is_image", "sha256", "file_size") if k in up})
        event = user_message_with_files(text, refs, message_uuid=message_uuid)
        return self.send_events(session_id, [event])

    def upload_file(
        self,
        file: str | os.PathLike | bytes,
        filename: str | None = None,
        mime: str | None = None,
        *,
        purpose: str = SEED_BUNDLE_PURPOSE,
        expires_in_seconds: int | None = None,
    ) -> dict:
        """``POST /v1/files`` (Files API, multipart ``file`` + ``purpose``
        [+ ``expires_in_seconds``], betas ``files-api-2025-04-14,
        oauth-2025-04-20``) -> ``{id: "file_…", …}``. For seed bundles
        (``config.seed_bundle_file_id``); chat attachments use
        :meth:`upload_attachment`."""
        data, name, kind = read_upload(file, filename, mime)
        form: dict[str, str] = {"purpose": purpose}
        if expires_in_seconds is not None:
            form["expires_in_seconds"] = str(expires_in_seconds)
        return self._request(
            "POST",
            FILES_PATH,
            data=form,
            files={"file": (name, data, kind)},
            web=False,
            org=False,
            betas=FILES_BETAS,
            accept=(200, 201),
        )

    def download_file(self, file_id: str) -> bytes:
        """``GET /v1/files/{id}/content`` — a Files API file's bytes."""
        return self._send("GET", f"{FILES_PATH}/{file_id}/content", web=False, org=False, betas=FILES_BETAS).content

    def delete_file(self, file_id: str) -> bool:
        """``DELETE /v1/files/{id}``: ``True`` when deleted, ``False`` when it
        was already gone (404)."""
        resp = self._send(
            "DELETE", f"{FILES_PATH}/{file_id}", web=False, org=False, betas=FILES_BETAS, accept=(200, 204, 404)
        )
        return resp.status_code != 404
