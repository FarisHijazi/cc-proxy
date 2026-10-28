"""File upload for sessions. Uploads need ``betas=FILES_BETAS``
(``files-api-2025-04-14``, ``oauth-2025-04-20``) and multipart bodies via
``files=`` (bytes, so a retry can resend).
"""

from __future__ import annotations


class FilesMixin:
    """File-upload methods for :class:`~claude_rc.client.RemoteControlClient`.

    The host (:class:`~claude_rc.client.RemoteControlClient`) provides, and these
    methods must use instead of any HTTP of their own:

    * ``self._request(method, path, *, json=, params=, betas=, client_platform=,
      org=, web=, headers=, accept=, retry=, timeout=, content=, data=, files=)``
      -> parsed body; ``self._send(...)`` -> checked :class:`httpx.Response`
      (``stream=True`` leaves it open). Both raise :class:`~claude_rc.client.APIError`.
    * ``self._path(*parts)`` -> ``/v1/code/sessions/...``; ``self.org_uuid``.
    * ``self.send_events``, ``self._send_control``, ``self.request_control``,
      ``self.wait_control_response``, ``self.list_events``, ``self.stream_events``.

    A method defined on ``RemoteControlClient`` itself shadows a mixin method of
    the same name.
    """
