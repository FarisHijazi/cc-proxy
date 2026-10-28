"""Scheduled tasks ("routines" = triggers) under ``/v1/code/triggers``.
These routes need ``betas=(TRIGGERS_BETA,)`` (``ccr-triggers-2026-01-30``).

A trigger starts a fresh cloud session on a schedule (``cron_expression``, 5
fields, UTC, at most hourly) or once (``run_once_at``, RFC3339 UTC; it then
disables itself with ``ended_reason: "run_once_fired"``). Each run is an
ordinary session: :meth:`TriggersMixin.trigger_runs` lists them and
``list_events`` reads a run's log.

**There is no delete.** The API the CLI uses has none, and the CLI tells users
to delete routines at ``https://claude.ai/code/routines``. Use
:meth:`TriggersMixin.disable_trigger` to stop one.

:func:`fire_routine` is separate: the official, per-routine-token API.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

import httpx

from ._version import __version__

_TRIGGERS = "/v1/code/triggers"
_TIMEOUT = 20.0  # the CLI's RemoteTrigger timeout
_MAX_PAGES = 50

#: Fields ``POST /v1/code/triggers/{id}`` accepts (CLI 2.1.312).
UPDATE_FIELDS = frozenset(
    {"name", "cron_expression", "run_once_at", "enabled", "job_config", "mcp_connections", "clear_mcp_connections"}
)

ROUTINES_URL = "https://claude.ai/code/routines"
ROUTINE_FIRE_BETA = "experimental-cc-routine-2026-04-01"
ROUTINE_API_BASE = "https://api.anthropic.com"


def _triggers_beta() -> str:
    # client.py imports this module, so read the constant at call time.
    from .client import TRIGGERS_BETA

    return TRIGGERS_BETA


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def rfc3339(when: datetime | str) -> str:
    """``when`` as RFC3339 UTC with a ``Z`` (``2026-11-01T09:00:00Z``); a
    string passes through. A naive datetime is taken as UTC."""
    if isinstance(when, str):
        return when
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def trigger_prompt_event(prompt: str, *, event_uuid: str | None = None) -> dict:
    """One ``job_config.ccr.events`` entry carrying the prompt the run starts
    with (the ``/schedule`` skill's form: ``{data:{uuid, session_id:"",
    type:"user", parent_tool_use_id:null, message:{content, role}}}``)."""
    return {
        "data": {
            "uuid": event_uuid or str(uuid.uuid4()),
            "session_id": "",
            "type": "user",
            "parent_tool_use_id": None,
            "message": {"content": prompt, "role": "user"},
        }
    }


def trigger_job_config(
    environment_id: str,
    prompt: str | None = None,
    *,
    model: str | None = None,
    repos: Iterable[str] = (),
    allowed_tools: Iterable[str] | None = None,
    session_context: dict | None = None,
    events: list[dict] | None = None,
) -> dict:
    """``{ccr:{environment_id, session_context, events}}``. ``repos`` are git
    URLs (``https://github.com/ORG/REPO``) turned into ``sources``;
    ``session_context`` entries override the built ones; ``events`` replaces
    the single prompt event."""
    ctx: dict[str, Any] = {}
    if model:
        ctx["model"] = model
    sources = [{"git_repository": {"url": url}} for url in repos]
    if sources:
        ctx["sources"] = sources
    if allowed_tools is not None:
        ctx["allowed_tools"] = list(allowed_tools)
    ctx.update(session_context or {})
    if events is None:
        events = [trigger_prompt_event(prompt)] if prompt is not None else []
    return {"ccr": {"environment_id": environment_id, "session_context": ctx, "events": events}}


def trigger_body(
    name: str,
    *,
    environment_id: str,
    prompt: str | None = None,
    cron_expression: str | None = None,
    run_once_at: datetime | str | None = None,
    enabled: bool = True,
    mcp_connections: Iterable[dict] | None = None,
    **job: Any,
) -> dict:
    """The ``POST /v1/code/triggers`` body. Exactly one of ``cron_expression``
    and ``run_once_at`` is required (:class:`ValueError` otherwise). ``job``
    goes to :func:`trigger_job_config` (``model``, ``repos``, ``allowed_tools``,
    ``session_context``, ``events``). ``mcp_connections`` items are
    ``{connector_uuid, name, url}``."""
    if (cron_expression is None) == (run_once_at is None):
        raise ValueError("pass exactly one of cron_expression or run_once_at")
    body: dict[str, Any] = {"name": name}
    if cron_expression is not None:
        body["cron_expression"] = cron_expression
    else:
        body["run_once_at"] = rfc3339(run_once_at)  # type: ignore[arg-type]
    body["enabled"] = enabled
    body["job_config"] = trigger_job_config(environment_id, prompt, **job)
    if mcp_connections is not None:
        body["mcp_connections"] = list(mcp_connections)
    return body


def trigger_items(body: Any) -> list[dict]:
    """Triggers out of a list response. The envelope is unverified
    (``{data}`` / ``{triggers}`` / bare list are all accepted)."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "triggers", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def trigger_prompt(trigger: dict) -> Optional[str]:
    """The prompt a trigger starts runs with. Reads both forms the server
    returns: ``job_config.ccr.events[].data.message`` (create form) and
    ``session_request.events[].payload.message`` (list/get form)."""
    ccr = (trigger.get("job_config") or {}).get("ccr") or {}
    candidates = [(e or {}).get("data") for e in ccr.get("events") or []]
    candidates += [(e or {}).get("payload") for e in (trigger.get("session_request") or {}).get("events") or []]
    for event in candidates:
        message = (event or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            if text:
                return text
    return None


def trigger_url(trigger_id: str) -> str:
    """Where claude.ai shows (and can delete) a routine."""
    return f"{ROUTINES_URL}/{trigger_id}"


# ---------------------------------------------------------------------------
# mixin
# ---------------------------------------------------------------------------
class TriggersMixin:
    """Scheduled-task (trigger) methods for :class:`~claude_rc.client.RemoteControlClient`.

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

    def _trigger_request(self, method: str, path: str, **kw: Any) -> Any:
        return self._request(method, path, betas=(_triggers_beta(),), timeout=_TIMEOUT, **kw)

    def iter_triggers(self) -> Iterator[dict]:
        """Yield every trigger from ``GET /v1/code/triggers``, following
        ``next_cursor`` as ``?cursor=`` when the server returns one (paging is
        unverified; the CLI reads a single page)."""
        params: dict[str, Any] = {}
        for _ in range(_MAX_PAGES):
            body = self._trigger_request("GET", _TRIGGERS, params=params or None)
            items = trigger_items(body)
            yield from items
            cursor = body.get("next_cursor") if isinstance(body, dict) else None
            if not items or not isinstance(cursor, str) or not cursor:
                return
            params = {"cursor": cursor}

    def list_triggers(self) -> list[dict]:
        """All scheduled tasks: ``{id, name, enabled, cron_expression,
        run_once_at, next_run_at, ended_reason?, session_request|job_config, …}``."""
        return list(self.iter_triggers())

    def get_trigger(self, trigger_id: str) -> dict:
        """``GET /v1/code/triggers/{id}``."""
        return self._trigger_request("GET", f"{_TRIGGERS}/{trigger_id}")

    def create_trigger(self, name: str, *, environment_id: str, prompt: str | None = None, **fields: Any) -> dict:
        """``POST /v1/code/triggers`` — a new scheduled task; returns it.
        ``fields`` are :func:`trigger_body`'s: ``cron_expression`` XOR
        ``run_once_at`` (required), ``enabled``, ``model``, ``repos``,
        ``allowed_tools``, ``session_context``, ``events``, ``mcp_connections``.
        Use :func:`~claude_rc.environments.default_environment` on
        ``list_environments()`` to pick ``environment_id``."""
        body = trigger_body(name, environment_id=environment_id, prompt=prompt, **fields)
        return self._trigger_request("POST", _TRIGGERS, json=body)

    def update_trigger(self, trigger_id: str, **fields: Any) -> dict:
        """``POST /v1/code/triggers/{id}`` with only the given fields (partial
        update). Allowed: :data:`UPDATE_FIELDS`; anything else raises
        :class:`ValueError`. A ``datetime`` ``run_once_at`` is converted."""
        unknown = set(fields) - UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unknown trigger fields: {sorted(unknown)}")
        if fields.get("run_once_at") is not None:
            fields["run_once_at"] = rfc3339(fields["run_once_at"])
        return self._trigger_request("POST", f"{_TRIGGERS}/{trigger_id}", json=fields)

    def enable_trigger(self, trigger_id: str) -> dict:
        """Resume a scheduled task (``{"enabled": true}``)."""
        return self.update_trigger(trigger_id, enabled=True)

    def disable_trigger(self, trigger_id: str) -> dict:
        """Pause a scheduled task (``{"enabled": false}``) — the closest thing
        to delete the API offers."""
        return self.update_trigger(trigger_id, enabled=False)

    def run_trigger(self, trigger_id: str, **body: Any) -> Any:
        """``POST /v1/code/triggers/{id}/run`` — fire now, outside the
        schedule. ``body`` is optional (the CLI forwards its tool input minus
        ``trigger_id``); the response shape is unverified."""
        return self._trigger_request("POST", f"{_TRIGGERS}/{trigger_id}/run", json=body)

    def create_webhook_trigger(self, routine_trigger_id: str, **body: Any) -> Any:
        """``POST /v1/code/webhook-triggers`` — attach an inbound webhook source
        (e.g. GitHub events on a repository) to a routine. **Body unverified**:
        the CLI's prompt only says it names the source and scope, an event list,
        a structured filter, and ``routine_trigger_id``; ``body`` is sent as-is
        next to ``routine_trigger_id``."""
        return self._trigger_request(
            "POST", "/v1/code/webhook-triggers", json={**body, "routine_trigger_id": routine_trigger_id}
        )

    def trigger_runs(self, trigger_id: str, *, limit: int = 10, cursor: str | None = None) -> dict:
        """``GET /v1/code/sessions?trigger_id={id}&limit=`` — the sessions a
        trigger started: ``{data:[{id, title, status, worker_status,
        created_at, last_event_at}], next_cursor}``. Read a run's log with
        ``list_events(run_id, sort_order="desc")``."""
        params: dict[str, Any] = {"trigger_id": trigger_id, "limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self._trigger_request("GET", self._path(), params=params)


# ---------------------------------------------------------------------------
# official routine fire API (separate credential)
# ---------------------------------------------------------------------------
def fire_routine(
    routine_id: str,
    token: str,
    text: str | None = None,
    *,
    base_url: str = ROUTINE_API_BASE,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 30.0,
) -> Any:
    """``POST {base_url}/v1/claude_code/routines/{id}/fire`` — run a routine
    with its own per-routine token (``Authorization: Bearer``,
    ``anthropic-beta: experimental-cc-routine-2026-04-01``), body ``{text}``
    (``{}`` without ``text``). Returns the parsed body; raises
    :class:`~claude_rc.client.APIError` on a non-2xx status. Never retried.

    This is the one deliberate exception to the client's single HTTP path:
    the routine token is not the session credential, so none of
    ``RemoteControlClient``'s auth, org or renewal logic applies. Response
    shape unverified (not in any capture)."""
    from .client import ANTHROPIC_VERSION, APIError

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": ROUTINE_FIRE_BETA,
        "Content-Type": "application/json",
        "User-Agent": f"claude-rc-api-python/{__version__}",
    }
    body = {"text": text} if text is not None else {}
    with httpx.Client(transport=transport, timeout=timeout) as http:
        resp = http.post(f"{base_url.rstrip('/')}/v1/claude_code/routines/{routine_id}/fire", headers=headers, json=body)
    if not 200 <= resp.status_code < 300:
        rid = resp.headers.get("request-id") or resp.headers.get("x-request-id")
        raise APIError(resp.status_code, resp.text, rid)
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text
