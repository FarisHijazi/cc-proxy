"""Environments: cloud environment providers, private org environments,
self-hosted runner pools, last-used environment.

An *environment* is where a session's worker runs: ``anthropic_cloud`` (a
claude.ai sandbox), ``bridge`` (a Remote Control machine — your own computer
running ``claude remote-control``), ``byoc`` / ``snap`` / ``ccs``. Start a
session on one by passing its ``environment_id`` to session create; a
self-hosted pool id (``ccpool_…``) goes in ``self_hosted_runner_pool_id``
instead (see :func:`environment_target`).

Environment objects can carry the environment's variables (``config.environment``).
Nothing here logs or prints them; :func:`redact_environment` gives a copy safe
to show.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Iterator, Optional, Sequence

#: Known ``kind`` values (CLI 2.1.312 enum).
ENVIRONMENT_KINDS = ("anthropic_cloud", "bridge", "byoc", "snap", "ccs")

#: Languages the CLI's default cloud environment installs.
DEFAULT_LANGUAGES = ({"name": "python", "version": "3.11"}, {"name": "node", "version": "20"})

_PROVIDERS = "/v1/environment_providers"
_POOLS = "/v1/code/runners/self-hosted/pools"
_MAX_PAGES = 50


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------
def environment_items(body: Any) -> list[dict]:
    """The environment list out of a list response: ``{environments: […]}``
    (``/v1/environment_providers``), a bare list, or ``{data: […]}``. The private
    web route's envelope is unverified, so every form is accepted."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("environments", "data", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def environments_of_kind(environments: Iterable[dict], *kinds: str) -> list[dict]:
    """Only the environments whose ``kind`` is one of ``kinds``.
    ``environments_of_kind(envs, "bridge")`` lists Remote Control machines."""
    return [e for e in environments if e.get("kind") in kinds]


def bridge_machines(environments: Iterable[dict]) -> list[dict]:
    """Remote Control machines: ``kind: "bridge"`` entries."""
    return environments_of_kind(environments, "bridge")


def default_environment(environments: Sequence[dict], preferred_id: str | None = None) -> Optional[dict]:
    """Pick an environment the way the CLI's teleport does: ``preferred_id``
    if present, else the first ``anthropic_cloud``, else the first non-bridge,
    else the first. ``None`` for an empty list (the CLI then auto-creates one,
    :meth:`EnvironmentsMixin.create_cloud_environment`)."""
    if preferred_id:
        for e in environments:
            if e.get("environment_id") == preferred_id:
                return e
    for pick in (
        lambda e: e.get("kind") == "anthropic_cloud",
        lambda e: e.get("kind") != "bridge",
        lambda e: True,
    ):
        for e in environments:
            if pick(e):
                return e
    return None


def environment_target(environment_id: str) -> dict:
    """The session-create field for an environment id: a ``ccpool_…`` id is a
    self-hosted pool (``self_hosted_runner_pool_id``), anything else is an
    ``environment_id`` (the CLI's rule)."""
    if environment_id.startswith("ccpool_"):
        return {"self_hosted_runner_pool_id": environment_id}
    return {"environment_id": environment_id}


def redact_environment(environment: dict) -> dict:
    """A deep copy with every environment-variable value replaced by
    ``"<redacted>"`` (keys kept), safe to print or log."""
    out = copy.deepcopy(environment)
    config = out.get("config")
    if isinstance(config, dict) and "environment" in config:
        env = config["environment"]
        config["environment"] = {k: "<redacted>" for k in env} if isinstance(env, dict) else "<redacted>"
    return out


def cloud_environment_body(
    name: str = "Default",
    *,
    description: str | None = None,
    cwd: str = "/home/user",
    init_script: str | None = None,
    environment: dict[str, str] | None = None,
    languages: Iterable[dict] | None = None,
    allowed_hosts: Iterable[str] = (),
    allow_default_hosts: bool = True,
    allow_mcp_servers: bool | None = None,
) -> dict:
    """The ``POST /v1/environment_providers/cloud/create`` body. All defaults
    reproduce the CLI's auto-created environment exactly (spec surface §2.2).

    ``allow_mcp_servers`` appears in the web app's environment object (HAR #28)
    but not in the CLI's create body; it is only sent when given (unverified on
    create). ``allowed_hosts=["*"]`` is what claude.ai shows as a fully open
    network."""
    network: dict[str, Any] = {"allowed_hosts": list(allowed_hosts), "allow_default_hosts": allow_default_hosts}
    if allow_mcp_servers is not None:
        network["allow_mcp_servers"] = allow_mcp_servers
    return {
        "name": name,
        "kind": "anthropic_cloud",
        "description": f"{name} - trusted network access" if description is None else description,
        "config": {
            "environment_type": "anthropic",
            "cwd": cwd,
            "init_script": init_script,
            "environment": dict(environment or {}),
            "languages": [dict(lang) for lang in (DEFAULT_LANGUAGES if languages is None else languages)],
            "network_config": network,
        },
    }


# ---------------------------------------------------------------------------
# mixin
# ---------------------------------------------------------------------------
class EnvironmentsMixin:
    """Environment and runner-pool methods for :class:`~claude_rc.client.RemoteControlClient`.

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

    # -- environment providers (CLI route) ----------------------------------
    def iter_environments(self, *, page_size: int = 100) -> Iterator[dict]:
        """Yield every environment from ``GET /v1/environment_providers``,
        following ``after_id`` = ``last_id`` while ``has_more`` is true (the
        CLI's paging; it stops on an empty page or a missing ``last_id``)."""
        params: dict[str, Any] = {"limit": page_size}
        for _ in range(_MAX_PAGES):
            body = self._request("GET", _PROVIDERS, params=params)
            items = environment_items(body)
            yield from items
            last_id = body.get("last_id") if isinstance(body, dict) else None
            if not items or not isinstance(body, dict) or body.get("has_more") is not True or not isinstance(last_id, str):
                return
            params = {"limit": page_size, "after_id": last_id}

    def list_environments(self, *, kind: str | Sequence[str] | None = None, page_size: int = 100) -> list[dict]:
        """All environments (every page), each ``{environment_id, name, kind, …}``.
        ``kind`` filters, e.g. ``kind="bridge"`` for Remote Control machines or
        ``kind=("anthropic_cloud", "byoc")``. Under cookie auth this is the web
        app's private org route: the public one answers 403 "Authentication
        method not allowed" to a cookie (live, 2026-10-28)."""
        auth = getattr(self, "_auth", None)
        if auth is not None and auth.is_cookie:
            envs = self.list_org_environments()
        else:
            envs = list(self.iter_environments(page_size=page_size))
        if kind is None:
            return envs
        return environments_of_kind(envs, *((kind,) if isinstance(kind, str) else kind))

    def create_cloud_environment(self, name: str = "Default", **options: Any) -> dict:
        """``POST /v1/environment_providers/cloud/create`` — a new claude.ai
        cloud environment; returns the environment object. ``options`` are
        :func:`cloud_environment_body`'s (``description``, ``cwd``,
        ``init_script``, ``environment`` vars, ``languages``, ``allowed_hosts``,
        ``allow_default_hosts``, ``allow_mcp_servers``); with none it is the
        CLI's default environment. Not idempotent: never retried on 5xx."""
        return self._request("POST", f"{_PROVIDERS}/cloud/create", json=cloud_environment_body(name, **options))

    # -- private org route (web app) ----------------------------------------
    def _private_environments_path(self, *parts: str) -> str:
        org = self.org_uuid
        if not org:
            raise ValueError("organization uuid unknown; pass org_uuid= to the client")
        return "/".join((f"{_PROVIDERS}/private/organizations/{org}/environments", *parts)).rstrip("/")

    def list_org_environments(self, *, limit: int = 1000) -> list[dict]:
        """``GET /v1/environment_providers/private/organizations/{org}/environments?limit=1000``
        — the web app's environment list (same data as :meth:`list_environments`).
        The response envelope is unverified; :func:`environment_items` accepts
        ``{environments}``, ``{data}`` or a bare list."""
        return environment_items(self._request("GET", self._private_environments_path(), params={"limit": limit}))

    def get_environment(self, environment_id: str) -> dict:
        """``GET /v1/environment_providers/private/organizations/{org}/environments/{id}``
        (web app, HAR #28) — the full object: ``{kind, environment_id, name,
        description, created_at, state, owner_account_uuid, config:{environment_type,
        sub_type, cwd, init_script, environment, languages, network_config:{allowed_hosts,
        allow_default_hosts, allow_mcp_servers}}, bridge_info, is_ccr_default}``.
        ``config.environment`` holds variable values: pass the result through
        :func:`redact_environment` before showing it."""
        return self._request("GET", self._private_environments_path(environment_id))

    def last_used_environment(self) -> Optional[dict]:
        """``GET /v1/code/last_used_environment`` — the environment the web app
        preselects, or ``None`` when the server has none (the captured call
        returned a plain-text 404, HAR #2). The 200 shape is unverified."""
        resp = self._send("GET", "/v1/code/last_used_environment", accept=(200, 404))
        if resp.status_code == 404:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        return body or None

    # -- self-hosted runner pools -------------------------------------------
    def list_self_hosted_pools(self) -> list[dict]:
        """``GET /v1/code/runners/self-hosted/pools`` — ``[{pool_id: "ccpool_…",
        name, created_at, alive_runner_count}]``. Start a session on a pool with
        :func:`environment_target` (``self_hosted_runner_pool_id``)."""
        body = self._request("GET", _POOLS)
        if isinstance(body, dict) and isinstance(body.get("pools"), list):
            return body["pools"]
        return body if isinstance(body, list) else []

    def get_self_hosted_pool(self, pool_id: str) -> dict:
        """``GET /v1/code/runners/self-hosted/pools/{pool_id}`` — one pool
        (``{pool}`` unwrapped). The CLI's admin tool sends this with neither the
        org header nor a beta, so neither is sent here."""
        body = self._request("GET", f"{_POOLS}/{pool_id}", web=False, org=False)
        return body["pool"] if isinstance(body, dict) and isinstance(body.get("pool"), dict) else body
