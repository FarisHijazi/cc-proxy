"""Clients for the Claude Remote Control / Sessions HTTP API.

Two entry points:

* :class:`RemoteControlClient` — talks to ``/v1/code/sessions`` with a claude.ai
  OAuth token, exactly like the claude.ai/code web app. Use it to list, steer,
  and observe Claude Code sessions running on a machine that started
  ``claude remote-control`` (or ``claude --remote-control``).

* :class:`ManagedAgentsClient` — talks to the public ``/v1/sessions`` Managed
  Agents API with an ``x-api-key``. Use it to create and drive fully
  cloud-hosted agent sessions.

Both were reverse-engineered from the Claude Code CLI binary. Nothing here is an
officially supported interface; endpoints and headers can change without notice.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

import httpx

from ._version import __version__
from .webauth import claude_ai_mounts
from .credentials import (
    CLAUDE_AI_BASE_URL,
    ENV_CLAUDE_AI_ORG_UUID,
    ENV_ORG_UUID,
    ENV_TRUSTED_DEVICE_TOKEN,
    OAUTH_BETA,
    AuthSource,
    CredentialsError,
    OAuthCredentials,
    load_org_uuid,
    org_uuid_from_profile,
    refresh_credentials,
    resolve_auth,
)
from .environments import EnvironmentsMixin
from .files import FilesMixin
from .interaction import InteractionMixin
from .lifecycle import LifecycleMixin
from .triggers import TriggersMixin
from .tts import TTSMixin
from .events import (
    RC,
    Event,
    _to_int,
    cli_control_cancel_request,
    cli_control_request,
    cli_permission_response,
    cli_question_response,
    cli_user_message,
    user_message,
)
from .sse import parse_sse
from . import webauth

DEFAULT_BASE_URL = "https://api.anthropic.com"
# Beta headers. The CLI sends none on /v1/code/*, but the claude.ai web app sends
# CCR_BETA on every /v1/code/* call (HAR 2026-10-28), so the web header set
# carries it; the CLI also uses it on the older /v1/sessions compat path.
CCR_BETA = "ccr-byoc-2025-07-29"
# Scheduled tasks ("routines") under /v1/code/triggers.
TRIGGERS_BETA = "ccr-triggers-2026-01-30"
# File uploads (Files API with an OAuth bearer).
FILES_BETAS = ("files-api-2025-04-14", OAUTH_BETA)
# The web app's `anthropic-client-feature` on /v1/code/* (HAR 2026-10-28).
CLIENT_FEATURE = "ccr"
MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
ANTHROPIC_VERSION = "2023-06-01"
# anthropic-client-platform value the claude.ai web app sends. Since Claude Code
# ~2.1.220 this header is load-bearing on ingest, not telemetry: CCR ingress
# stamps it into each ingested event's `client_platform`, and the worker treats
# a user message as coming from a *human controller* only when that value is
# one of `ios` / `android` / `web_claude_ai` / `desktop_app`. Any other value
# (including the `claude_code_remote` this library sent before 0.2.1) gets the
# message demoted to *peer* origin — "Another Claude session sent a message"
# framing — and, depending on the worker's peer-gate policy, parked without
# ever starting a turn (observed live on 2.1.234; 2.1.219 predates the gate).
CLIENT_PLATFORM = "web_claude_ai"
USER_AGENT = f"claude-rc-api-python/{__version__}"

# Retries (spec §9): idempotent requests retry transient failures (network,
# 5xx) after 2/4/8/16 s; a 429 is retried for any method (it was not accepted),
# honouring `retry-after` up to _MAX_RETRY_AFTER. A non-idempotent POST that
# failed in flight is never resent — it may already be stored.
_MAX_RETRIES = 4
_RETRY_BASE = 2.0
_MAX_RETRY_AFTER = 60.0
_IDEMPOTENT = frozenset({"GET", "HEAD", "OPTIONS"})

# Waiting for a control verdict: the history page size each poll scans, and how
# far the poll interval backs off for slow verdicts (a side question can take
# minutes).
_VERDICT_SCAN = 50
_MAX_POLL_INTERVAL = 2.0


def _backoff_sleep(attempt: int, base: float = 0.5, cap: float = 30.0) -> None:
    time.sleep(min(cap, base * (2 ** (attempt - 1))))


def _retry_delay(attempt: int, resp: httpx.Response | None = None) -> float:
    """Seconds before retry ``attempt`` (1-based): the server's ``retry-after``
    when it gave one (seconds or HTTP date, capped), else 2/4/8/16 s."""
    header = resp.headers.get("retry-after") if resp is not None else None
    if header:
        try:
            return max(0.0, min(_MAX_RETRY_AFTER, float(header)))
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                return max(0.0, min(_MAX_RETRY_AFTER, when.timestamp() - time.time()))
            except (TypeError, ValueError):
                pass
    return _RETRY_BASE * (2 ** (attempt - 1))


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        for part in str(item).split(","):
            if part.strip():
                seen.setdefault(part.strip(), None)
    return list(seen)


def build_headers(
    *,
    token: str | None = None,
    session_key: str | None = None,
    cookie: str | None = None,
    org_uuid: str | None = None,
    web: bool = True,
    betas: Sequence[str] = (),
    client_platform: str | None = None,
    trusted_device_token: str | None = None,
    json_body: bool = True,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The one header set every RemoteControlClient request uses.

    ``web=True`` (the default) is what claude.ai/code sends on every
    ``/v1/code/*`` call: ``anthropic-version``, ``anthropic-beta:
    ccr-byoc-2025-07-29`` (+ ``betas``), ``anthropic-client-platform:
    web_claude_ai`` (what makes a worker treat a message as a human's, spec
    §8), ``anthropic-client-feature: ccr``. ``web=False`` is the bare OAuth set
    (``/api/oauth/profile``): no platform, feature or CCR beta — only ``betas``.
    Auth is ``Authorization: Bearer`` or claude.ai cookies: ``cookie`` (a full
    ``Cookie`` header from :class:`~claude_rc.webauth.CookieJar`) or a bare
    ``session_key``; cookie auth sends no ``Authorization`` and a browser
    ``User-Agent``. ``x-organization-uuid`` rides when ``org_uuid`` is
    given. ``json_body=False`` leaves ``Content-Type`` to httpx (multipart).
    ``extra`` is applied last and wins.
    """
    h: dict[str, str] = {"anthropic-version": ANTHROPIC_VERSION, "User-Agent": USER_AGENT}
    if cookie is None and session_key is not None:
        cookie = f"sessionKey={session_key}"
    if cookie is not None:
        h["Cookie"] = cookie
        h["User-Agent"] = webauth.user_agent()
    elif token is not None:
        h["Authorization"] = f"Bearer {token}"
    if json_body:
        h["Content-Type"] = "application/json"
    beta = _dedupe([CCR_BETA, *betas] if web else betas)
    if beta:
        h["anthropic-beta"] = ",".join(beta)
    if web or client_platform:
        h["anthropic-client-platform"] = client_platform or CLIENT_PLATFORM
    if web:
        h["anthropic-client-feature"] = CLIENT_FEATURE
    if org_uuid:
        h["x-organization-uuid"] = org_uuid
    if trusted_device_token:
        h["X-Trusted-Device-Token"] = trusted_device_token
    if extra:
        h.update(extra)
    return h


def _parse(resp: httpx.Response) -> Any:
    """A checked response's body: JSON, else text, ``None`` when empty."""
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        return resp.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return resp.text


def _ack_sequence(ack: Any) -> Optional[int]:
    """The sequence number ingest assigned the first event of a send."""
    results = ack.get("results") if isinstance(ack, dict) else None
    if isinstance(results, list) and results and isinstance(results[0], dict):
        return _to_int(results[0].get("sequence_num"))
    return None


def _unwrap_session(body: Any) -> Any:
    """Session reads and updates wrap the object (``{response_shape: …}`` /
    ``{session: …}``); hand callers the session itself."""
    if isinstance(body, dict):
        for key in ("response_shape", "session"):
            if isinstance(body.get(key), dict):
                return body[key]
    return body


class APIError(RuntimeError):
    """A non-accepted HTTP status. ``error`` is the Anthropic error envelope's
    ``error`` object when the body was one (``{type, message, resource?,
    sub_reason?, …}``, spec §9), else ``{}``."""

    def __init__(self, status: int, body: str, request_id: Optional[str] = None):
        self.status = status
        self.body = body
        self.request_id = request_id
        super().__init__(f"HTTP {status}{f' [{request_id}]' if request_id else ''}: {body[:300]}")

    @property
    def error(self) -> dict:
        try:
            parsed = json.loads(self.body)
        except (json.JSONDecodeError, TypeError):
            return {}
        err = parsed.get("error") if isinstance(parsed, dict) else None
        return err if isinstance(err, dict) else {}

    @property
    def error_type(self) -> Optional[str]:
        """``error.type``, e.g. ``session_not_active`` (409 on a send)."""
        return self.error.get("type")

    @property
    def resource(self) -> Optional[str]:
        """``error.resource``, e.g. ``untrusted_device`` / ``session_stale_relogin`` (403)."""
        return self.error.get("resource")


class ControlRejected(RuntimeError):
    """The worker answered a ``control_request`` with an error ``control_response``.

    The ingest POST succeeding only means the request was queued — the worker
    acks (or refuses) asynchronously in the event stream. A subtype the worker
    doesn't serve comes back as ``"REPL bridge does not handle control_request
    subtype: …"``."""


class ControlTimeout(TimeoutError):
    """No ``control_response`` to a ``control_request`` arrived in time — the
    worker may be busy, gone, or on a version that drops the subtype."""

    def __init__(self, request_id: str, message: str):
        super().__init__(message)
        self.request_id = request_id


# ---------------------------------------------------------------------------
# OAuth (Remote Control) client
# ---------------------------------------------------------------------------
class RemoteControlClient(
    LifecycleMixin, EnvironmentsMixin, TriggersMixin, InteractionMixin, FilesMixin, TTSMixin
):
    """Controller-side client for Remote Control sessions (``/v1/code/sessions``).

    This plays the same role as the browser at claude.ai/code: it does not run
    Claude, it *steers* a session that is running on someone's machine.

    Every HTTP call goes through :meth:`_send` (headers from
    :func:`build_headers`, 401 recovery, §9 retries, :class:`APIError`);
    :meth:`_request` is the same call returning the parsed body. Feature
    groups live in the mixins this class inherits.
    """

    def __init__(
        self,
        credentials: OAuthCredentials | None = None,
        org_uuid: str | None = None,
        *,
        token: str | None = None,
        session_key: str | None = None,
        cswap_slot: str | int | None = None,
        auth: AuthSource | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        credentials_path: str | None = None,
        config_path: str | None = None,
        auto_refresh: bool = True,
        persist_refresh: bool = True,
        client_id: str | None = None,
        client_platform: str = CLIENT_PLATFORM,
        timeout: float = 30.0,
    ):
        """Credentials come from :func:`~claude_rc.credentials.resolve_auth`
        (``credentials`` / ``token`` / ``session_key`` / ``cswap_slot``, then the
        environment, then ``~/.claude/.credentials.json``), or ``auth`` as
        resolved. Cookie auth (:mod:`claude_rc.webauth`) is the default whenever
        a cookie source yields a ``sessionKey``. ``base_url`` defaults to
        ``https://api.anthropic.com``, or ``https://claude.ai`` under cookie
        auth. ``transport`` replaces httpx's network transport
        (``httpx.MockTransport`` in tests). The org UUID is ``org_uuid``, else
        env, else the ``lastActiveOrg`` cookie (cookie auth) or
        ``~/.claude.json`` (bearer), else looked up on first use
        (:attr:`org_uuid`)."""
        self._auth = auth or resolve_auth(
            token,
            credentials=credentials,
            session_key=session_key,
            cswap_slot=cswap_slot,
            credentials_path=credentials_path,
        )
        self._creds = self._auth.credentials
        default_base = CLAUDE_AI_BASE_URL if self._auth.is_cookie else DEFAULT_BASE_URL
        self.base_url = (base_url or default_base).rstrip("/")
        self._creds_path = credentials_path
        if self._auth.is_cookie:
            # ~/.claude.json names the CLI login's org, not the browser's.
            self._org_uuid = (
                org_uuid
                or os.environ.get(ENV_CLAUDE_AI_ORG_UUID)
                or os.environ.get(ENV_ORG_UUID)
                or self._auth.jar.org_uuid
            )
        else:
            self._org_uuid = org_uuid or load_org_uuid(config_path)
        self._org_looked_up = bool(self._org_uuid)
        self._auto_refresh = auto_refresh
        self._persist_refresh = persist_refresh
        self.client_platform = client_platform
        # A stable per-instance id for presence, like the web client.
        self.client_id = client_id or str(uuid.uuid4())
        self._http = httpx.Client(
            timeout=httpx.Timeout(timeout, read=timeout), transport=transport, mounts=claude_ai_mounts(transport)
        )
        self._refresher = httpx.Client(timeout=30.0, transport=transport)

    # -- context management ------------------------------------------------
    def __enter__(self) -> "RemoteControlClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()
        self._refresher.close()

    # -- auth --------------------------------------------------------------
    @property
    def cookie_jar(self) -> Optional["webauth.CookieJar"]:
        """The live claude.ai cookie jar under cookie auth, else ``None``."""
        return self._auth.jar

    def cookie_header(self) -> Optional[str]:
        """The current ``Cookie`` header (rotated values included), for other
        transports such as the TTS websocket; ``None`` under bearer auth."""
        return self._auth.jar.header() if self._auth.jar is not None else None

    def _reload_credentials(self) -> bool:
        """Re-read the credential source; ``True`` if it held different tokens.

        Tokens are rotated *in place* by whichever process refreshes first (the
        Claude Code CLI itself, another client of this library, cswap …), and a
        refresh token is single-use — once someone else has spent ours, the
        source is the only valid copy. A long-lived client must therefore
        treat the file (or cswap slot), not its memory, as the source of truth.
        No-op when there is nothing to re-read (env var / explicit token)."""
        reload = self._auth.reload
        if reload is None or self._creds is None:
            return False
        try:
            fresh = reload()
        except CredentialsError:
            return False
        if (
            fresh.access_token == self._creds.access_token
            and fresh.refresh_token == self._creds.refresh_token
        ):
            return False
        self._creds = self._auth.credentials = fresh
        return True

    def _refresh(self) -> None:
        self._creds = self._auth.credentials = refresh_credentials(
            self._creds, persist=self._persist_refresh, client=self._refresher
        )

    def _token(self) -> str:
        if self._creds is None:
            raise CredentialsError("cookie-authenticated client has no bearer token")
        if self._auto_refresh and self._creds.is_expired():
            # Prefer the source copy: another process may already have rotated
            # the tokens, which invalidated our (single-use) refresh token.
            if not self._reload_credentials() or self._creds.is_expired():
                try:
                    self._refresh()
                except CredentialsError:
                    # Our refresh token was already spent (invalid_grant) —
                    # someone else may have written fresh tokens meanwhile.
                    if not self._reload_credentials() or self._creds.is_expired():
                        raise
        return self._creds.access_token

    def _renew_after_401(self) -> bool:
        """Recover from a 401 once, mirroring the CLI's bridge client: fresh
        tokens at the source (rotated by another process) win; otherwise spend
        our refresh token. ``False`` when nothing changed (cookie / bare token)."""
        if not self._auto_refresh or self._creds is None:
            return False
        if self._reload_credentials():
            return True
        if not self._creds.refresh_token:
            return False
        try:
            self._refresh()
            return True
        except CredentialsError:
            if self._reload_credentials():
                return True
            raise

    @property
    def org_uuid(self) -> Optional[str]:
        """The organization UUID for ``x-organization-uuid``: explicit, env
        (``CLAUDE_AI_ORG_UUID`` / ``CLAUDE_RC_ORG_UUID``), ``~/.claude.json``,
        else fetched once and cached — ``GET /api/oauth/profile``
        (``organization.uuid``, needs ``user:profile``) for a bearer, or
        ``GET /api/organizations`` (first entry's ``uuid``; **unverified**
        shape) under cookie auth. ``None`` when the lookup fails."""
        if not self._org_looked_up:
            self._org_looked_up = True
            try:
                if self._auth.is_cookie:
                    body = self._request("GET", "/api/organizations", org=False, web=False)
                    first = body[0] if isinstance(body, list) and body else {}
                    self._org_uuid = first.get("uuid") if isinstance(first, dict) else None
                else:
                    body = self._request(
                        "GET", "/api/oauth/profile", org=False, web=False,
                        headers={"Cache-Control": "no-cache"},
                    )
                    self._org_uuid = org_uuid_from_profile(body)
            except (APIError, CredentialsError, httpx.HTTPError):
                self._org_uuid = None
        return self._org_uuid

    def _headers(
        self,
        extra: Mapping[str, str] | None = None,
        *,
        web: bool = True,
        betas: Sequence[str] = (),
        client_platform: str | None = None,
        org: bool = True,
        json_body: bool = True,
    ) -> dict:
        """Headers for one request under the given route policy (see
        :meth:`_send`); also what a websocket upgrade (TTS) should send."""
        trusted = (self._creds.trusted_device_token if self._creds else None) or os.environ.get(
            ENV_TRUSTED_DEVICE_TOKEN
        )
        return build_headers(
            token=None if self._auth.is_cookie else self._token(),
            cookie=self.cookie_header(),
            org_uuid=self.org_uuid if org else None,
            web=web,
            betas=betas,
            client_platform=client_platform or (self.client_platform if web else None),
            trusted_device_token=trusted,
            json_body=json_body,
            extra=extra,
        )

    def _path(self, *parts: Any) -> str:
        """``/v1/code/sessions[/part…]`` — the path form :meth:`_send` takes."""
        tail = "/".join(str(p).strip("/") for p in parts if p != "")
        return "/v1/code/sessions" + (f"/{tail}" if tail else "")

    def _url(self, *parts: Any) -> str:
        """Absolute URL of :meth:`_path` (kept for callers of 0.3.0)."""
        return self.base_url + self._path(*parts)

    def _abs(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else self.base_url + "/" + path.lstrip("/")

    # -- the narrow waist ---------------------------------------------------
    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | str | None = None,
        data: Mapping[str, Any] | None = None,
        files: Any = None,
        headers: Mapping[str, str] | None = None,
        web: bool = True,
        betas: Sequence[str] = (),
        client_platform: str | None = None,
        org: bool = True,
        accept: Sequence[int] = (200, 201, 204),
        retry: bool | None = None,
        stream: bool = False,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.Response:
        """Send one request; the only place this client touches HTTP.

        ``path`` is relative to :attr:`base_url` (``/v1/code/...``) or an
        absolute URL. Route policy: ``web`` / ``betas`` / ``client_platform`` /
        ``org`` pick the headers (:func:`build_headers`); ``headers`` adds or
        overrides. Bodies: ``json``, or ``content`` / ``data`` / ``files``
        (multipart — pass bytes, not file objects, so a retry can resend).

        A 401 triggers one credential renewal and resend. Under cookie auth
        every claude.ai ``Set-Cookie`` is merged into the jar, a 401/403
        re-reads the cookie source once and resends if it changed, and a
        Cloudflare challenge raises
        :class:`~claude_rc.webauth.CloudflareChallenge`. A 429 is retried for
        any method; network errors and 5xx only when ``retry`` (default: GET /
        HEAD / OPTIONS). Any status outside ``accept`` raises :class:`APIError`.
        With ``stream=True`` the body is left unread: iterate it, then close the
        response (``with contextlib.closing(client._send(..., stream=True)) as resp:``).
        """
        method = method.upper()
        idempotent = method in _IDEMPOTENT if retry is None else retry
        json_body = files is None and data is None and content is None
        attempt = 0
        renewed = False
        while True:
            request = self._http.build_request(
                method,
                self._abs(path),
                params=params,
                json=json,
                content=content,
                data=data,
                files=files,
                headers=self._headers(
                    headers, web=web, betas=betas, client_platform=client_platform, org=org, json_body=json_body
                ),
                **({"timeout": timeout} if timeout is not None else {}),
            )
            try:
                resp = self._http.send(request, stream=stream)
            except httpx.TransportError:
                if not idempotent or attempt >= _MAX_RETRIES:
                    raise
                attempt += 1
                time.sleep(_retry_delay(attempt))
                continue
            status = resp.status_code
            jar = self._auth.jar
            if jar is not None:
                jar.merge(resp)
                if status in (401, 403) and webauth.is_cloudflare_challenge(resp):
                    resp.close()
                    raise webauth.challenge_error(resp)
            again = None
            if status == 401 and not renewed and self._renew_after_401():
                renewed, again = True, 0.0
            elif status in (401, 403) and jar is not None and not renewed and jar.reread():
                renewed, again = True, 0.0
            elif status == 429 and attempt < _MAX_RETRIES:
                attempt += 1
                again = _retry_delay(attempt, resp)
            elif status >= 500 and idempotent and attempt < _MAX_RETRIES:
                attempt += 1
                again = _retry_delay(attempt, resp)
            if again is not None:
                resp.close()
                if again:
                    time.sleep(again)
                continue
            if status not in accept:
                body = resp.read().decode(errors="replace") if stream else resp.text
                resp.close()
                rid = resp.headers.get("request-id") or resp.headers.get("x-request-id")
                raise APIError(status, body, rid)
            return resp

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        """:meth:`_send` (same arguments), returning the parsed body: JSON,
        else text, ``None`` when empty."""
        return _parse(self._send(method, path, **kw))

    @staticmethod
    def _ok(resp: httpx.Response, *accept: int) -> Any:
        """Check-and-parse for an already received response (0.3.0 helper)."""
        accepted = accept or (200, 201, 204)
        if resp.status_code not in accepted:
            rid = resp.headers.get("request-id") or resp.headers.get("x-request-id")
            raise APIError(resp.status_code, resp.text, rid)
        return _parse(resp)

    # -- sessions ----------------------------------------------------------
    def list_sessions(self, *, limit: int | None = None, cursor: str | None = None) -> dict:
        """``GET /v1/code/sessions`` — your Remote Control sessions, newest first:
        ``{data, next_cursor}``. Pass ``next_cursor`` back as ``cursor`` for the
        next page."""
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if cursor is not None:
            params["cursor"] = cursor
        return self._request("GET", self._path(), params=params)

    def sessions(self, **kw) -> list[dict]:
        """Convenience: just the list of session objects from :meth:`list_sessions`."""
        body = self.list_sessions(**kw)
        if isinstance(body, list):
            return body
        for key in ("data", "sessions", "results", "items"):
            if isinstance(body.get(key), list):
                return body[key]
        return []

    def get_session(self, session_id: str) -> dict:
        """``GET /v1/code/sessions/{id}`` — the session object (unwrapped from the
        ``response_shape`` envelope the endpoint returns)."""
        return _unwrap_session(self._request("GET", self._path(session_id)))

    def update_session(self, session_id: str, **fields: Any) -> dict:
        """``PUT /v1/code/sessions/{id}`` — change ``title``, or ``add_tags`` /
        ``remove_tags``. Returns the updated session."""
        return _unwrap_session(self._request("PUT", self._path(session_id), json=fields))

    def rename_session(self, session_id: str, title: str) -> dict:
        """Retitle a session where every controller's list shows it (a connected
        worker adopts the new title too)."""
        return self.update_session(session_id, title=title)

    def archive_session(self, session_id: str) -> bool:
        """``POST /v1/code/sessions/{id}/archive`` — end/hide a session. 409 = already archived."""
        self._send("POST", self._path(session_id, "archive"), json={}, accept=(200, 201, 204, 409))
        return True

    # -- sending events (steering) ----------------------------------------
    def send_events(self, session_id: str, events: Iterable[dict]) -> dict:
        """``POST /v1/code/sessions/{id}/events``.

        Body is ``{session_id, events:[{payload: <event+uuid>}]}`` — each event is
        wrapped in ``payload`` and given a ``uuid`` if it lacks one (matching the CLI).
        The ack is ``{results: [{event_id, sequence_num, duplicate}], attestation_feedback}``.
        """
        wrapped = []
        for e in events:
            if "uuid" not in e:
                e = {**e, "uuid": str(uuid.uuid4())}
            wrapped.append({"payload": e})
        body = {"session_id": session_id, "events": wrapped}
        return self._request("POST", self._path(session_id, "events"), json=body, accept=(200, 201))

    def send_message(self, session_id: str, text: str, *, message_uuid: str | None = None) -> dict:
        """Send a user message (Claude Code stream-json ``user`` event) — what you
        type at claude.ai/code. Pass ``message_uuid`` to keep a handle for
        :meth:`cancel_queued_message`."""
        return self.send_events(session_id, [cli_user_message(text, message_uuid=message_uuid)])

    def send_raw(self, session_id: str, payload: dict) -> dict:
        """Send an arbitrary stream-json payload (advanced escape hatch)."""
        return self.send_events(session_id, [payload])

    def cancel_queued_message(self, session_id: str, message_uuid: str, *, timeout: float = 15.0) -> bool:
        """Withdraw a message the worker hasn't started on yet — one sent with
        ``message_uuid`` that is queued behind a running turn or prompt. True if
        it was removed; False once a turn has picked it up."""
        response = self.request_control(session_id, "cancel_async_message", message_uuid=message_uuid, timeout=timeout)
        return bool(response.get("cancelled"))

    # -- answering the worker's blocking prompts ----------------------------
    def respond_permission(
        self,
        session_id: str,
        request_id: str,
        *,
        allow: bool,
        tool_input: dict | None = None,
        tool_use_id: str | None = None,
        message: str | None = None,
    ) -> dict:
        """Allow or deny a ``can_use_tool`` prompt (``Event.is_blocking_control``).
        See :func:`~claude_rc.events.cli_permission_response` for the fields."""
        answer = cli_permission_response(
            request_id, allow=allow, tool_input=tool_input, tool_use_id=tool_use_id, message=message
        )
        return self.send_events(session_id, [answer])

    def answer_question(
        self,
        session_id: str,
        request_id: str,
        answers: dict[str, str | list[str]],
        *,
        tool_input: dict,
        tool_use_id: str | None = None,
    ) -> dict:
        """Answer an ``AskUserQuestion`` prompt: ``answers`` maps question text to
        the chosen label(s); ``{}`` dismisses it. See
        :func:`~claude_rc.events.cli_question_response`."""
        answer = cli_question_response(request_id, answers, tool_input=tool_input, tool_use_id=tool_use_id)
        return self.send_events(session_id, [answer])

    # -- the control protocol -----------------------------------------------
    def _send_control(self, session_id: str, subtype: str, **fields: Any) -> tuple[str, dict]:
        request_id = f"{subtype}-{uuid.uuid4()}"
        ack = self.send_events(session_id, [cli_control_request(subtype, request_id=request_id, **fields)])
        return request_id, ack

    def send_control(self, session_id: str, subtype: str, **fields: Any) -> str:
        """Send a ``control_request`` without waiting; returns its request id
        (pair with :meth:`wait_control_response`)."""
        return self._send_control(session_id, subtype, **fields)[0]

    def _control(self, session_id: str, subtype: str, timeout: float, **fields: Any) -> tuple[str, dict]:
        """Send a control and wait for its verdict: ``(request_id, verdict)``."""
        request_id, ack = self._send_control(session_id, subtype, **fields)
        verdict = self.wait_control_response(
            session_id, request_id, timeout=timeout, after_sequence_num=_ack_sequence(ack)
        )
        if verdict is None:
            raise ControlTimeout(request_id, f"no answer to {subtype} within {timeout:g}s")
        if verdict.get("subtype") == "error":
            raise ControlRejected(str(verdict.get("error") or f"{subtype} rejected"))
        return request_id, verdict

    def request_control(self, session_id: str, subtype: str, *, timeout: float = 15.0, **fields: Any) -> dict:
        """Send a ``control_request`` and wait for the worker's verdict.

        Returns the verdict's ``response`` payload (``{}`` when it has none).
        Raises :class:`ControlRejected` on an error verdict and
        :class:`ControlTimeout` when none arrives within ``timeout``. The typed
        methods below wrap the subtypes worth having; this reaches the rest
        (``read_file``, ``file_suggestions``, ``mcp_status`` …, see
        API_REFERENCE.md).
        """
        return self._control(session_id, subtype, timeout, **fields)[1].get("response") or {}

    def wait_control_response(
        self,
        session_id: str,
        request_id: str,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.4,
        after_sequence_num: int | None = None,
    ) -> dict | None:
        """Poll history for the worker's ``control_response`` to ``request_id``.

        Returns the ``response`` object (``subtype`` is ``success`` | ``error``),
        or ``None`` if none arrived within ``timeout``. Controls are acked
        asynchronously through the event stream — a 200 on the ingest POST only
        means "queued", so this is how a caller learns whether the worker
        actually applied one. ``after_sequence_num`` (the request's own, from
        the send ack) makes the scan exact; without it each poll checks the
        newest events. The interval backs off for slow verdicts.
        """
        deadline = time.monotonic() + timeout
        delay = poll_interval
        cursor = after_sequence_num
        while True:
            if cursor is None:
                page = self.list_events(session_id, limit=_VERDICT_SCAN, sort_order="desc")
            else:
                page = self.list_events(session_id, limit=_VERDICT_SCAN, sort_order="asc", cursor=str(cursor))
            for ev in page:
                if ev.type == RC.CONTROL_RESPONSE and ev.request_id == request_id:
                    return ev.payload.get("response") or {}
            if cursor is not None:
                cursor = max([cursor, *(ev.sequence_num or 0 for ev in page)])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if cursor is not None and len(page) == _VERDICT_SCAN:
                continue  # a full page: more history is already waiting
            time.sleep(min(delay, remaining))
            delay = min(delay * 1.5, _MAX_POLL_INTERVAL)

    def interrupt(self, session_id: str) -> dict:
        """Interrupt the running agent via a ``control_request`` (jumps the queue)."""
        req = cli_control_request("interrupt", request_id=f"interrupt-{uuid.uuid4()}")
        return self.send_events(session_id, [req])

    def set_model(self, session_id: str, model: str) -> dict:
        """Change the session's model (e.g. ``claude-opus-5-5``)."""
        req = cli_control_request("set_model", request_id=f"set-model-{uuid.uuid4()}", model=model)
        return self.send_events(session_id, [req])

    def set_permission_mode(self, session_id: str, mode: str) -> dict:
        """Set permission mode: ``default`` | ``plan`` | ``acceptEdits`` | ``bypassPermissions``."""
        req = cli_control_request(
            "set_permission_mode", request_id=f"set-mode-{uuid.uuid4()}", mode=mode
        )
        return self.send_events(session_id, [req])

    def set_effort(
        self,
        session_id: str,
        effort: str | None,
        *,
        ultracode: bool = False,
        wait: float | None = None,
        command_fallback: bool = False,
    ) -> dict:
        """Set reasoning effort: ``low`` | ``medium`` | ``high`` | ``xhigh`` | ``max``;
        ``None`` = auto (the model's default).

        There is no ``set_effort`` control subtype — the CLI's ``/effort`` command
        sends ``apply_flag_settings`` with ``settings.effortLevel`` (an explicit
        ``null`` clears back to auto) and ``ultracode`` alongside it (a plain
        effort change switches ultracode off; ``ultracode=True`` needs ``xhigh``
        and an xhigh-capable model). Applied this way the level is
        session-scoped: the worker re-emits ``system/init`` with the new
        ``effort`` and persists nothing.

        Remote-control workers refused ``apply_flag_settings`` through at least
        2.1.212 (``"REPL bridge does not handle…"``); 2.1.301 applies it. For
        older workers, two opt-ins:

        * ``wait`` (seconds) — poll for the worker's ``control_response`` and act
          on the verdict instead of firing blind. Without it, a refusal only ever
          surfaces in the event stream.
        * ``command_fallback`` — on refusal, inject ``/effort <level>`` as a user
          message instead. Workers run slash commands from controllers as local
          commands (zero cost, no model turn) — but with CLI semantics: a plain
          level is also *saved as that machine's default for the model*, not
          session-scoped. Requires ``wait``. Without it a refusal raises
          :class:`ControlRejected`.

        Returns ``{"via": "control" | "command" | "control_unconfirmed", "ack": …}``
        — ``control_unconfirmed`` means ``wait`` elapsed with no verdict (assumed
        applied; no fallback is attempted, to avoid a double apply).
        """
        request_id, ack = self._send_control(
            session_id, "apply_flag_settings", settings={"effortLevel": effort, "ultracode": ultracode}
        )
        if wait is None:
            return {"via": "control", "ack": ack}
        resp = self.wait_control_response(
            session_id, request_id, timeout=wait, after_sequence_num=_ack_sequence(ack)
        )
        if resp is None:
            return {"via": "control_unconfirmed", "ack": ack}
        if resp.get("subtype") != "error":
            return {"via": "control", "ack": ack}
        if not command_fallback:
            raise ControlRejected(str(resp.get("error") or "control request rejected"))
        command = "ultracode" if ultracode else (effort or "auto")
        ack = self.send_message(session_id, f"/effort {command}")
        return {"via": "command", "ack": ack}

    # -- asking the worker --------------------------------------------------
    def initialize(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """The controller handshake claude.ai sends on every attach. Returns the
        worker's ``claude_code_version``, ``current_model``,
        ``current_permission_mode`` … plus ``pending_permission_requests`` /
        ``pending_user_dialog_requests`` (the prompts it is blocked on). Like an
        attach, it makes the worker post a fresh ``system/init`` event."""
        verdict = self._control(session_id, "initialize", timeout)[1]
        return {
            **(verdict.get("response") or {}),
            "pending_permission_requests": verdict.get("pending_permission_requests") or [],
            "pending_user_dialog_requests": verdict.get("pending_user_dialog_requests") or [],
        }

    def side_question(
        self,
        session_id: str,
        question: str,
        *,
        history: list[dict] | None = None,
        timeout: float = 300.0,
    ) -> str | None:
        """Ask the session something on the side — claude.ai's "btw". The worker
        answers from its conversation without interrupting a running turn or
        adding to the transcript (it does cost a model call).

        ``history`` holds this side thread's earlier ``{question, response}``
        pairs, so follow-ups make sense. Returns the answer, or None when the
        worker has nothing to answer from (e.g. an empty conversation). On
        ``timeout`` the question is withdrawn and :class:`ControlTimeout` raised.
        """
        fields: dict[str, Any] = {"question": question}
        if history:
            fields["history"] = [{"question": h["question"], "response": h["response"]} for h in history]
        try:
            verdict = self._control(session_id, "side_question", timeout, **fields)[1]
        except ControlTimeout as exc:
            try:  # don't leave the worker answering a question nobody waits for
                self.send_events(session_id, [cli_control_cancel_request(exc.request_id)])
            except APIError:
                pass
            raise
        return (verdict.get("response") or {}).get("response")

    def get_context_usage(self, session_id: str, *, detail: str = "summary", timeout: float = 15.0) -> dict:
        """How full the context window is: ``totalTokens`` / ``maxTokens`` /
        ``percentage``, ``autoCompactThreshold``, and a per-``categories``
        breakdown. The headline numbers also ride the session object
        (``external_metadata.context_usage``) with no round trip."""
        return self.request_control(session_id, "get_context_usage", detail=detail, timeout=timeout)

    def get_usage(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """Plan usage as the worker's account sees it: ``rate_limits.limits`` (the
        5-hour and weekly windows, each ``{kind, percent, resets_at}``), the
        ``session``'s cost and line totals, and ``subscription_type``."""
        return self.request_control(session_id, "get_usage", timeout=timeout)

    def get_workspace_diff(self, session_id: str, *, timeout: float = 15.0) -> dict | None:
        """The worker's uncommitted git changes — ``stats`` (``filesCount``,
        ``linesAdded``, ``linesRemoved``), ``perFileStats``, ``hunks`` — or None
        outside a git repo. A diff still computing after ~8 s is refused with
        ``workspace_diff_pending`` (raised as :class:`ControlRejected`)."""
        return self.request_control(session_id, "get_workspace_diff", timeout=timeout).get("diff")

    def stop_task(self, session_id: str, task_id: str, *, timeout: float = 15.0) -> None:
        """Stop a running shell/agent task by the ``task_id`` of its
        ``system/task_started`` event (``system/background_tasks_changed`` lists
        the running background set). The worker acks unknown ids too; a stopped
        task reports ``task_notification`` with ``status: stopped``."""
        self.request_control(session_id, "stop_task", task_id=task_id, timeout=timeout)

    def mark_read(
        self, session_id: str, up_to_sequence_num: int | None = None, *, event_id: str | None = None
    ) -> Any:
        """``POST /v1/code/sessions/{id}/mark_read`` — mark the session read.

        The web app sends ``{event_id}`` (the newest event's uuid) and gets
        ``{unread: false}`` (HAR 2026-10-28); ``up_to_sequence_num`` sends the
        0.3.0 ``{sequence_num}`` form instead (unverified)."""
        body: dict[str, Any] = {}
        if event_id is not None:
            body["event_id"] = event_id
        if up_to_sequence_num is not None:
            body["sequence_num"] = up_to_sequence_num
        return self._request("POST", self._path(session_id, "mark_read"), json=body, accept=(200, 204))

    # -- reading events: polling ------------------------------------------
    def list_events(
        self,
        session_id: str,
        *,
        limit: int | None = None,
        sort_order: str | None = None,
        cursor: str | None = None,
    ) -> list[Event]:
        """``GET /v1/code/sessions/{id}/events`` — paginated history (returns immediately).

        ``sort_order`` is ``asc`` (oldest first — the session's FIRST events) or
        ``desc`` (newest first, what the CLI fetches). ``cursor`` is a sequence
        number: ``asc`` returns the events after it, ``desc`` the events before
        it (each response's ``next_cursor`` is the last one it held).
        """
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if sort_order is not None:
            params["sort_order"] = sort_order
        if cursor is not None:
            params["cursor"] = cursor
        body = self._request("GET", self._path(session_id, "events"), params=params)
        rows = body if isinstance(body, list) else body.get("data") or body.get("events") or []
        return [Event.from_wire(r) for r in rows]

    # -- reading events: streaming (SSE) ----------------------------------
    def stream_events(
        self,
        session_id: str,
        *,
        from_sequence_num: int = 0,
        reconnect: bool = True,
        max_reconnects: int = 20,
    ) -> Iterator[Event]:
        """``GET /v1/code/sessions/{id}/events/stream`` — live SSE events.

        Replays every persisted event after ``from_sequence_num`` (exclusive — 0
        replays the whole backlog), then follows live. A ``client_event`` frame is
        an envelope ``{event_type, sequence_num, source, payload}``; this unwraps
        ``payload`` into an :class:`Event`. ``ephemeral_event`` frames (partial
        ``stream_event`` deltas, ``system/thinking_tokens``) come through with no
        sequence number; ``session_update`` / ``delivery_update`` frames are
        skipped. Resumes with both ``?from_sequence_num=`` and a ``Last-Event-ID``
        header, de-duping by ``sequence_num``, so a dropped connection neither
        replays nor skips events. Reconnects with exponential backoff.
        """
        seen: set[int] = set()
        last_seq = from_sequence_num
        attempts = 0
        while True:
            params: dict[str, Any] = {"from_sequence_num": last_seq}
            headers = {"Accept": "text/event-stream"}
            if last_seq:
                headers["Last-Event-ID"] = str(last_seq)
            try:
                with closing(self._send(
                    "GET", self._path(session_id, "events", "stream"), headers=headers, params=params,
                    accept=(200,), retry=False, stream=True, timeout=httpx.Timeout(30.0, read=None),
                )) as resp:
                    attempts = 0
                    for frame in parse_sse(resp.iter_lines()):
                        if frame.is_heartbeat:
                            continue
                        # The RC stream marks each frame's sequence_num on the id: line.
                        if frame.id:
                            try:
                                last_seq = max(last_seq, int(frame.id))
                            except ValueError:
                                pass
                        # Ignore purely informational top-level events.
                        if frame.event in ("session_update", "delivery_update"):
                            continue
                        try:
                            data = json.loads(frame.data)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        # `catch_up_truncated` signals a history gap; surface it raw.
                        if frame.event == "catch_up_truncated" or "payload" not in data:
                            yield Event.from_wire(data)
                            continue
                        ev = Event.from_wire(data)
                        if ev.sequence_num is not None:
                            if ev.sequence_num in seen:
                                continue
                            seen.add(ev.sequence_num)
                            last_seq = max(last_seq, ev.sequence_num)
                        yield ev
            except httpx.HTTPError:
                if not reconnect or attempts >= max_reconnects:
                    raise
                attempts += 1
                _backoff_sleep(attempts)
                continue
            if not reconnect:
                return

    # -- presence ----------------------------------------------------------
    def presence(self, session_id: str, *, clear: bool = False) -> None:
        """``POST /v1/code/sessions/{id}/client/presence`` — announce (or clear) that a
        controller is watching. The web client pulses this periodically."""
        if clear:
            payload = {"client_id": self.client_id, "clear": True}
        else:
            payload = {
                "client_id": self.client_id,
                "connected_at": datetime.now(timezone.utc).isoformat(),
            }
        self._request("POST", self._path(session_id, "client", "presence"), json=payload, accept=(200, 204))

    # -- high-level convenience -------------------------------------------
    def send_and_collect(
        self,
        session_id: str,
        text: str,
        *,
        print_stream: bool = False,
    ) -> list[Event]:
        """Send a message and stream until the session goes idle again.

        Returns every event received during the turn. This is the "ask and wait
        for the answer" pattern.

        Nothing is lost to the gap between sending and connecting. :meth:`stream_events`
        is a generator, so the HTTP connection only opens on the first iteration
        below — *after* the send — but the stream is resumed from the newest
        sequence number that existed beforehand, so the server replays anything
        that happened in between.
        """
        # Seed the resume cursor from the newest event so we only see new ones.
        try:
            history = self.list_events(session_id, limit=1, sort_order="desc")
            start = max((e.sequence_num or 0) for e in history) if history else 0
        except APIError:
            start = 0

        collected: list[Event] = []
        stream = self.stream_events(session_id, from_sequence_num=start, reconnect=False)
        self.send_message(session_id, text)
        for ev in stream:
            collected.append(ev)
            if print_stream and ev.role == "assistant":
                t = ev.text()
                if t:
                    print(t)
            # Stop when the turn ends, the session dies, or it blocks on us
            # (a permission prompt / dialog that needs a controller response).
            if ev.is_turn_end or ev.is_terminal or ev.is_blocking_control:
                break
        return collected


# ---------------------------------------------------------------------------
# API-key (public Managed Agents) client
# ---------------------------------------------------------------------------
class ManagedAgentsClient:
    """Client for the public Managed Agents Sessions API (``/v1/sessions``).

    Unlike Remote Control, sessions here run in Anthropic's cloud. You must
    create an agent (and usually an environment) first. Events are sent
    *unwrapped* (``{"events": [event, ...]}``) and the SSE stream frames carry
    the event object directly (``id``/``processed_at``, no ``sequence_num``).
    """

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, read=timeout))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._http.close()

    def close(self):
        self._http.close()

    def _headers(self, betas: list[str] | None = None, extra: dict | None = None) -> dict:
        beta = ",".join([MANAGED_AGENTS_BETA, *(betas or [])])
        h = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": beta,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if extra:
            h.update(extra)
        return h

    def _ok(self, resp: httpx.Response, *accept: int) -> Any:
        accepted = accept or (200, 201, 204)
        if resp.status_code not in accepted:
            rid = resp.headers.get("request-id")
            raise APIError(resp.status_code, resp.text, rid)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # -- agents / environments --------------------------------------------
    def create_agent(self, **body) -> dict:
        return self._ok(self._http.post(f"{self.base_url}/v1/agents", headers=self._headers(), json=body))

    def create_environment(self, **body) -> dict:
        return self._ok(self._http.post(f"{self.base_url}/v1/environments", headers=self._headers(), json=body))

    # -- sessions ----------------------------------------------------------
    def create_session(self, **body) -> dict:
        return self._ok(self._http.post(f"{self.base_url}/v1/sessions", headers=self._headers(), json=body))

    def list_sessions(self, **params) -> dict:
        return self._ok(self._http.get(f"{self.base_url}/v1/sessions", headers=self._headers(), params=params))

    def get_session(self, session_id: str) -> dict:
        return self._ok(self._http.get(f"{self.base_url}/v1/sessions/{session_id}", headers=self._headers()))

    def archive_session(self, session_id: str) -> Any:
        return self._ok(self._http.post(f"{self.base_url}/v1/sessions/{session_id}/archive", headers=self._headers(), json={}))

    def delete_session(self, session_id: str) -> Any:
        return self._ok(self._http.delete(f"{self.base_url}/v1/sessions/{session_id}", headers=self._headers()))

    # -- events ------------------------------------------------------------
    def send_events(self, session_id: str, events: list[dict]) -> dict:
        return self._ok(
            self._http.post(
                f"{self.base_url}/v1/sessions/{session_id}/events",
                headers=self._headers(),
                json={"events": events},
            )
        )

    def send_message(self, session_id: str, text: str) -> dict:
        return self.send_events(session_id, [user_message(text)])

    def list_events(self, session_id: str, **params) -> list[Event]:
        body = self._ok(
            self._http.get(f"{self.base_url}/v1/sessions/{session_id}/events", headers=self._headers(), params=params)
        )
        rows = body.get("data") if isinstance(body, dict) else body
        return [Event.from_wire(r) for r in (rows or [])]

    def stream_events(self, session_id: str, *, event_deltas: list[str] | None = None) -> Iterator[Event]:
        params = {}
        if event_deltas:
            params["event_deltas[]"] = event_deltas
        with self._http.stream(
            "GET",
            f"{self.base_url}/v1/sessions/{session_id}/events/stream",
            headers=self._headers(extra={"Accept": "text/event-stream"}),
            params=params,
            timeout=httpx.Timeout(30.0, read=None),
        ) as resp:
            if resp.status_code != 200:
                # Read the body first: on a streaming response `_ok` would touch
                # `.text` before it exists and raise httpx.ResponseNotRead
                # instead of the APIError callers are told to expect.
                body = resp.read().decode(errors="replace")
                raise APIError(resp.status_code, body, resp.headers.get("request-id"))
            for frame in parse_sse(resp.iter_lines()):
                if frame.is_heartbeat:
                    continue
                try:
                    yield Event.from_wire(json.loads(frame.data))
                except json.JSONDecodeError:
                    continue
