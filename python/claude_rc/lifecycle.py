"""Session lifecycle: create (environment, model, effort, repo/branch), list,
get, rename, tag, archive, unarchive, delete, share, heartbeat, mark read.

Wire shapes come from Claude Code CLI 2.1.312 (``spec/sessions.md``) and a
sanitized claude.ai/code HAR of 2026-10-28 (``spec/har-2026-10-28.md``); a
docstring says "unverified" where neither shows the shape.

Session ids exist in two spellings for the same session: ``cse_X`` (v2) and
``session_X`` (compat; what claude.ai shows in URLs). The web app reads with
``session_X`` and writes events / presence / stream / heartbeat with ``cse_X``;
the server accepts either on ``/v1/code/sessions/{id}``. :func:`to_cse_id` and
:func:`to_session_id` swap the prefix.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .events import cli_control_request

#: Beta the CLI adds on claude.ai-hosted ``/api/organizations/...`` OAuth routes.
OAUTH_ROUTE_BETA = "oauth-2025-04-20"
#: Env vars the CLI strips from ``environment_variables`` before a create.
STRIPPED_ENV_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SESSIONS_OAUTH_TOKEN")
#: The CLI's fallback outcome branch when no title gives a slug (``surface.md`` §4.1).
DEFAULT_BRANCH = "claude/task"
#: Prefix of a self-hosted runner pool id; such ids go in ``self_hosted_runner_pool_id``.
POOL_PREFIX = "ccpool_"
_ID_PREFIXES = ("cse_", "session_")


# -- pure helpers ---------------------------------------------------------------
def to_cse_id(session_id: str) -> str:
    """``session_X`` -> ``cse_X`` (other ids unchanged): the write-side spelling."""
    return "cse_" + session_id[len("session_"):] if session_id.startswith("session_") else session_id


def to_session_id(session_id: str) -> str:
    """``cse_X`` -> ``session_X`` (other ids unchanged): the read / URL spelling."""
    return "session_" + session_id[len("cse_"):] if session_id.startswith("cse_") else session_id


def session_web_url(session_id: str) -> str:
    """``https://claude.ai/code/session_X`` — where a person opens the session."""
    return f"https://claude.ai/code/{to_session_id(session_id)}"


def repo_slug(repo: str) -> str:
    """``owner/name`` from ``owner/name``, ``https://github.com/owner/name[.git]``
    or ``git@github.com:owner/name.git``."""
    s = repo.strip()
    s = re.sub(r"^(?:https?://|ssh://)?(?:[^@/]+@)?github\.com[:/]", "", s)
    s = s.removesuffix("/").removesuffix(".git")
    return s


def repo_url(repo: str) -> str:
    """``https://github.com/owner/name`` — the ``config.sources[].url`` form."""
    return f"https://github.com/{repo_slug(repo)}"


def branch_for(text: str | None) -> str:
    """A default outcome branch, ``claude/<slug>`` from ``text`` (title or
    prompt), else :data:`DEFAULT_BRANCH`. The server appends its own random
    suffix (HAR: ``claude/lucid-brown`` came back ``claude/lucid-brown-7cmwr7``)."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())[:6]
    slug = "-".join(words)[:40].strip("-")
    return f"claude/{slug}" if slug else DEFAULT_BRANCH


def environment_target(environment_id: str) -> dict:
    """``{environment_id}`` or, for a ``ccpool_`` id, ``{self_hosted_runner_pool_id}``
    (the CLI's ``phn``)."""
    key = "self_hosted_runner_pool_id" if environment_id.startswith(POOL_PREFIX) else "environment_id"
    return {key: environment_id}


def git_source(repo: str, revision: str | None = None) -> dict:
    """One ``config.sources`` entry: the repo to clone, at ``revision`` (branch or sha)."""
    src: dict[str, Any] = {"type": "git_repository", "url": repo_url(repo)}
    if revision:
        src["revision"] = revision
    return src


def git_outcome(repo: str, branches: str | Sequence[str]) -> dict:
    """One ``config.outcomes`` entry: the GitHub branch(es) Claude pushes to."""
    names = [branches] if isinstance(branches, str) else list(branches)
    return {"type": "git_repository", "git_info": {"type": "github", "repo": repo_slug(repo), "branches": names}}


def first_prompt(content: str | list, *, session_id: str = "") -> dict:
    """The first user message as the web / CLI send it: ``content`` stays a plain
    string (HAR #18) or a block list. ``session_id`` is ``""`` inside a create
    body (CLI §3.4) and the ``session_X`` id when sent afterwards (web)."""
    return {
        "type": "user",
        "uuid": str(uuid.uuid4()),
        "session_id": session_id,
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": content},
    }


def initial_events(
    *,
    permission_mode: str | None = None,
    max_thinking_tokens: int | None = None,
    extra: Iterable[dict] = (),
) -> list[dict]:
    """The control events a create starts with, in the CLI's order (spec §3.4):
    ``set_max_thinking_tokens``, ``set_permission_mode``, then ``extra`` raw
    payloads. The first prompt goes last and is added by the caller."""
    out: list[dict] = []
    if max_thinking_tokens is not None:
        out.append(cli_control_request(
            "set_max_thinking_tokens", f"set-thinking-{uuid.uuid4()}", max_thinking_tokens=max_thinking_tokens))
    if permission_mode is not None:
        out.append(cli_control_request("set_permission_mode", f"set-mode-{uuid.uuid4()}", mode=permission_mode))
    out.extend(extra)
    return out


def _unwrap_session(body: Any) -> Any:
    # client.py imports this module, so its helpers are looked up at call time.
    from .client import _unwrap_session as unwrap

    return unwrap(body)


def _api_error() -> type:
    from .client import APIError

    return APIError


def _with_uuid(event: dict) -> dict:
    return event if "uuid" in event else {**event, "uuid": str(uuid.uuid4())}


def _clean_env(env: Mapping[str, str] | None) -> dict:
    return {k: v for k, v in (env or {}).items() if k not in STRIPPED_ENV_VARS}


def _repo_branch(item: Any) -> dict:
    if isinstance(item, Mapping):
        return dict(item)
    repo, branch = item
    return {"repo": repo_slug(repo), "branch": branch}


class LifecycleMixin:
    """Session lifecycle methods for :class:`~claude_rc.client.RemoteControlClient`.

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
    the same name (``list_sessions``, ``get_session``, ``update_session``,
    ``rename_session``, ``archive_session``, ``mark_read`` live there).
    """

    # -- create -----------------------------------------------------------------
    def create_session(
        self,
        environment_id: str,
        prompt: str | list | None = None,
        *,
        title: str | None = None,
        repo: str | None = None,
        revision: str | None = None,
        branch: str | Sequence[str] | None = None,
        sources: Sequence[dict] | None = None,
        outcomes: Sequence[dict] | None = None,
        model: str | None = None,
        effort_level: str | None = None,
        append_system_prompt: str | None = None,
        permission_mode: str | None = None,
        max_thinking_tokens: int | None = None,
        events: Iterable[dict] = (),
        tags: Sequence[str] | None = None,
        session_grouping_id: str | None = None,
        environment_variables: Mapping[str, str] | None = None,
        defer_events: bool = False,
        legacy: bool = False,
        body_extra: Mapping[str, Any] | None = None,
        **config: Any,
    ) -> dict:
        """Start a cloud session: ``POST /v1/code/sessions`` (v1alpha2, spec §3.2).

        * ``environment_id``: an ``env_…`` id, or a ``ccpool_…`` self-hosted
          runner pool (sent as ``self_hosted_runner_pool_id``).
        * ``repo`` (``owner/name`` or GitHub URL) adds ``config.sources``
          (cloned at ``revision``) and ``config.outcomes`` pushing to ``branch``,
          default ``claude/<slug of title or prompt>`` (the server appends a
          suffix). ``sources`` / ``outcomes`` replace these for multi-repo work.
        * ``model``, ``effort_level`` (``low|medium|high|xhigh|max``),
          ``append_system_prompt`` and any other ``config`` field the spec lists
          (``cwd``, ``allowed_tools``, ``disallowed_tools``, ``builtin_tools``,
          ``reuse_outcome_branches``, ``seed_bundle_file_id``, ``fallback_model``,
          ``max_turns``, ``max_budget_usd``, ``output_schema``, ``correlation_id``,
          ``custom_system_prompt`` …) pass through ``**config`` verbatim.
          ``body_extra`` adds top-level fields (``agent_id`` …).
        * ``environment_variables`` minus the OAuth tokens the CLI strips.

        Initial events (spec §3.4): ``set_max_thinking_tokens``,
        ``set_permission_mode`` (``default|acceptEdits|plan|auto|…``; the create
        body has no ``permission_mode`` field), raw ``events``, then ``prompt``
        as the first user message. By default they ride in the create body's
        ``events`` like the CLI does (one request; the server stores them before
        the worker starts). ``defer_events=True`` mirrors the web app instead
        (HAR #16/#18): create with ``events: []``, then send them all in one
        ``/events`` POST to the ``cse_`` id. Device attestation is never sent
        (we cannot sign); an org that requires bound sessions will refuse.

        Returns the session object (unwrapped from ``{deduplicated, session}``).

        ``legacy=True`` uses the old ``POST /v1/sessions`` instead (spec §3.3):
        the same config under ``session_context``, events as
        ``[{type: "event", data}]``, the bare session as response. Unverified
        against a live server (the CLI uses it only behind a feature gate).
        """
        cfg: dict[str, Any] = {}
        if sources is not None or repo:
            cfg["sources"] = list(sources) if sources is not None else [git_source(repo, revision)]
        if outcomes is not None or repo:
            cfg["outcomes"] = (
                list(outcomes) if outcomes is not None
                else [git_outcome(repo, branch or branch_for(title or (prompt if isinstance(prompt, str) else None)))]
            )
        if model:
            cfg["model"] = model
        if effort_level:
            cfg["effort_level"] = effort_level
        if append_system_prompt:
            cfg["append_system_prompt"] = append_system_prompt
        cfg.update({k: v for k, v in config.items() if v is not None})
        cfg.setdefault("sources", [])
        cfg.setdefault("outcomes", [])

        evs = initial_events(permission_mode=permission_mode, max_thinking_tokens=max_thinking_tokens, extra=events)
        env = _clean_env(environment_variables)

        if legacy:
            return self._create_session_legacy(
                environment_id, cfg, evs + ([first_prompt(prompt)] if prompt is not None else []),
                title=title, tags=tags, env=env, body_extra=body_extra)

        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        body.update(environment_target(environment_id))
        body["config"] = cfg
        inline = [] if defer_events else evs + ([first_prompt(prompt)] if prompt is not None else [])
        body["events"] = [{"payload": _with_uuid(e)} for e in inline]
        if tags:
            body["tags"] = list(tags)
        if session_grouping_id:
            body["session_grouping_id"] = session_grouping_id
        if env:
            body["environment_variables"] = env
        body.update(body_extra or {})
        session = _unwrap_session(self._request("POST", self._path(), json=body, accept=(200, 201)))
        if defer_events and (evs or prompt is not None) and isinstance(session, dict) and session.get("id"):
            sid = session["id"]
            later = evs + ([first_prompt(prompt, session_id=to_session_id(sid))] if prompt is not None else [])
            self.send_events(to_cse_id(sid), later)
        return session

    def _create_session_legacy(
        self,
        environment_id: str,
        cfg: dict,
        evs: list[dict],
        *,
        title: str | None,
        tags: Sequence[str] | None,
        env: dict,
        body_extra: Mapping[str, Any] | None,
    ) -> dict:
        """``POST /v1/sessions`` (spec §3.3; unverified): v1 envelope."""
        ctx = dict(cfg)
        if env:
            ctx["environment_variables"] = env
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        body["events"] = [{"type": "event", "data": _with_uuid(e)} for e in evs]
        body["session_context"] = ctx
        body.update(environment_target(environment_id))
        if tags:
            body["tags"] = list(tags)
        body.update(body_extra or {})
        return _unwrap_session(self._request("POST", "/v1/sessions", json=body, accept=(200, 201)))

    def generate_title_and_branch(
        self, first_message: str, *, model: str | None = None, title_style: str | None = "default"
    ) -> dict:
        """``POST /api/organizations/{org}/dust/generate_title_and_branch`` ->
        ``{title, branch_name}`` (HAR #17; the CLI calls it on api.anthropic.com
        with a bearer and ``anthropic-beta: oauth-2025-04-20``, ``surface.md``
        §4.1). Feed the result to :meth:`create_session` (``title``, ``branch``)
        or to :meth:`rename_session` after a warm create, as the web app does."""
        body: dict[str, Any] = {"first_session_message": first_message}
        if title_style:
            body["title_style"] = title_style
        if model:
            body["model"] = model
        return self._request(
            "POST", f"/api/organizations/{self.org_uuid}/dust/generate_title_and_branch",
            json=body, web=False, betas=(OAUTH_ROUTE_BETA,), client_platform=self.client_platform)

    # -- list -------------------------------------------------------------------
    def query_sessions(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        statuses: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        exclude_tags: str | Sequence[str] | None = None,
        trigger_id: str | None = None,
        include_trigger_sessions: bool | None = None,
        **params: Any,
    ) -> dict:
        """``GET /v1/code/sessions`` with filters -> ``{data, next_cursor?, resume_token?}``.

        ``statuses`` repeats (``&statuses=active&statuses=paused``, spec §4.1 and
        the web capture). ``tags`` repeats the same way: the web sent one
        ``tags=cowork-remote``; several values is unverified. ``exclude_tags="-"``
        and ``include_trigger_sessions=True`` are what the watch feed passes;
        ``trigger_id`` lists one scheduled task's runs. Extra ``params`` pass
        through."""
        q: dict[str, Any] = {}
        if limit is not None:
            q["limit"] = limit
        if cursor is not None:
            q["cursor"] = cursor
        if statuses:
            q["statuses"] = list(statuses)
        if tags:
            q["tags"] = list(tags)
        if exclude_tags is not None:
            q["exclude_tags"] = exclude_tags if isinstance(exclude_tags, str) else list(exclude_tags)
        if trigger_id is not None:
            q["trigger_id"] = trigger_id
        if include_trigger_sessions is not None:
            q["include_trigger_sessions"] = "true" if include_trigger_sessions else "false"
        q.update(params)
        return self._request("GET", self._path(), params=q)

    def iter_sessions(self, *, max_pages: int | None = None, **filters: Any) -> Iterator[dict]:
        """Every session :meth:`query_sessions` matches, following ``next_cursor``
        until it is absent (or ``max_pages``)."""
        cursor = filters.pop("cursor", None)
        page = 0
        while True:
            body = self.query_sessions(cursor=cursor, **filters)
            rows = body if isinstance(body, list) else (body or {}).get("data") or []
            yield from rows
            page += 1
            cursor = body.get("next_cursor") if isinstance(body, dict) else None
            if not cursor or (max_pages is not None and page >= max_pages):
                return

    # -- update / tags / archive / delete --------------------------------------
    def tag_session(
        self, session_id: str, *, add: Sequence[str] = (), remove: Sequence[str] = ()
    ) -> dict:
        """``PUT /v1/code/sessions/{id}`` ``{add_tags, remove_tags}`` (spec §4.3) ->
        the updated session. Tags such as ``color:<name>``,
        ``config:auto-create-pr:off``."""
        fields: dict[str, Any] = {}
        if add:
            fields["add_tags"] = list(add)
        if remove:
            fields["remove_tags"] = list(remove)
        return _unwrap_session(self._request("PUT", self._path(session_id), json=fields))

    def unarchive_session(self, session_id: str) -> Any:
        """``POST /v1/code/sessions/{id}/unarchive`` with ``{}`` (spec §4.4). A 403
        carries ``error.resource`` ``untrusted_device`` / ``session_stale_relogin``
        (see :attr:`~claude_rc.client.APIError.resource`). Returns the parsed
        body, unwrapped when it is a session."""
        return _unwrap_session(self._request("POST", self._path(session_id, "unarchive"), json={}))

    def delete_session(self, session_id: str, *, legacy_fallback: bool = True) -> bool:
        """Delete a session for good. **Route unverified**: the web UI has
        Delete but no capture shows its request, and the CLI has none (spec
        §4.4, §13). Tries ``DELETE /v1/code/sessions/{id}``; on 404/405, and when
        ``legacy_fallback``, ``DELETE /v1/sessions/{id}`` (old family, which takes
        the ``session_`` spelling). Returns True on 200/202/204."""
        ok = (200, 202, 204)
        try:
            self._send("DELETE", self._path(session_id), accept=ok)
            return True
        except _api_error() as exc:
            if not legacy_fallback or exc.status not in (404, 405):
                raise
        self._send("DELETE", f"/v1/sessions/{to_session_id(session_id)}", accept=ok)
        return True

    # -- share ------------------------------------------------------------------
    def get_share(self, session_id: str) -> dict:
        """``GET /v1/code/sessions/{id}/share`` (HAR #24) -> ``{effective_visibility:
        "private"…, prospective_visibility_type: "sign_in_only"…,
        sharing_settings, current_last_sequence_num}``."""
        return self._request("GET", self._path(session_id, "share"))

    def set_share(self, session_id: str, *, method: str = "PUT", **fields: Any) -> Any:
        """Change who can view a session (the Share dialog's Private / Public).

        **Unverified**: no capture shows the write. This sends ``fields`` as JSON
        to ``{method} /v1/code/sessions/{id}/share``; the read side suggests a
        field like ``visibility_type`` (values seen: ``sign_in_only``) or a
        ``sharing_settings`` object. Check with :meth:`get_share` afterwards."""
        return self._request(method, self._path(session_id, "share"), json=fields)

    # -- liveness ---------------------------------------------------------------
    def heartbeat_check(self, session_ids: Iterable[str]) -> dict:
        """``POST /v1/code/sessions/heartbeat_check`` ``{session_ids: [cse_…]}``
        (HAR #13; the web polls it every 15-30 s) -> ``{checks_enabled,
        results: [{session_id, outcome: "fresh"…, last_heartbeat_at}]}``. Ids
        are converted to the ``cse_`` spelling the web sends."""
        body = {"session_ids": [to_cse_id(s) for s in session_ids]}
        return self._request("POST", self._path("heartbeat_check"), json=body, retry=True)

    # -- transcript (teleport) --------------------------------------------------
    def teleport_events(
        self,
        session_id: str,
        *,
        limit: int = 1000,
        cursor: str | None = None,
        from_first_event: bool = False,
        subagents: bool = False,
        session_agent_id: str | None = None,
    ) -> dict:
        """One page of ``GET /v1/code/sessions/{id}/teleport-events`` (spec §4.10)
        -> ``{data: [{payload: <transcript JSONL entry>, session_agent_id?}],
        next_cursor}``. These are transcript rows (what ``--teleport`` restores),
        not stream events."""
        q: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            q["cursor"] = cursor
        if from_first_event:
            q["from_first_event"] = "true"
        if session_agent_id is not None:
            q["session_agent_id"] = session_agent_id
        if subagents:
            q["subagents"] = "true"
        return self._request("GET", self._path(session_id, "teleport-events"), params=q)

    def iter_teleport_events(
        self, session_id: str, *, max_pages: int = 100, from_first_event: bool = True, **kw: Any
    ) -> Iterator[dict]:
        """Every transcript row, paging like the CLI (≤ ``max_pages``, default
        100). If ``from_first_event`` is refused (400, or 403 with
        ``resource: from_first_event``) the first page is retried without it."""
        cursor = None
        for page in range(max_pages):
            try:
                body = self.teleport_events(session_id, cursor=cursor, from_first_event=from_first_event, **kw)
            except _api_error() as exc:
                refused = exc.status == 400 or (exc.status == 403 and exc.resource == "from_first_event")
                if not (from_first_event and page == 0 and refused):
                    raise
                from_first_event = False
                body = self.teleport_events(session_id, cursor=cursor, **kw)
            yield from (body or {}).get("data") or []
            cursor = (body or {}).get("next_cursor")
            if not cursor:
                return

    # -- move / github ----------------------------------------------------------
    def move_to_cloud(
        self,
        session_id: str,
        environment_id: str,
        *,
        outcome_branch: str | None = None,
        permission_mode: str | None = None,
        revision_fallback: Any = None,
    ) -> dict:
        """``POST /v1/code/sessions/{id}/move-to-cloud`` (spec §4.12, CLI
        ``a_0288003392``): move a Remote Control (bridge) session into a cloud
        environment. Body ``{environment_id, outcome_branch?, permission_mode?,
        revision_fallback?}`` (the source-revision / memory-mode helpers are
        empty in 2.1.312). Refusals: 400 "not a bridge session",
        ``move_permission_mode_refused``. Returns the unwrapped session."""
        body: dict[str, Any] = {"environment_id": environment_id}
        if outcome_branch:
            body["outcome_branch"] = outcome_branch
        if permission_mode:
            body["permission_mode"] = permission_mode
        if revision_fallback:
            body["revision_fallback"] = revision_fallback
        return _unwrap_session(self._request("POST", self._path(session_id, "move-to-cloud"), json=body))

    def batch_branch_status(
        self,
        repo_branches: Iterable[Any] = (),
        *,
        session_ids: Iterable[str] | None = None,
        discover_session_prs: bool | None = None,
        include_review_decision: bool | None = None,
        include_compare: bool | None = None,
        caller: str | None = None,
    ) -> list[dict]:
        """``POST /v1/code/github/get-batch-branch-status`` (HAR #1/#29, spec
        ``surface.md`` §4.1) -> the ``branch_statuses`` list: ``{repo, branch,
        branch_exists, commits, has_changes, default_branch,
        has_session_binding, address_comments_muted}``.

        ``repo_branches`` are ``(repo, branch)`` pairs or ``{repo, branch}``
        dicts. ``session_ids`` (``session_`` spelling, as the web sends) with
        ``discover_session_prs=True`` asks for those sessions' PRs. ``caller``
        is the web's ``?caller=`` tag (``sessions-provider`` …), optional."""
        body: dict[str, Any] = {"repo_branches": [_repo_branch(rb) for rb in repo_branches]}
        if discover_session_prs is not None:
            body["discover_session_prs"] = discover_session_prs
        if session_ids is not None:
            body["session_ids"] = [to_session_id(s) for s in session_ids]
        if include_review_decision is not None:
            body["include_review_decision"] = include_review_decision
        if include_compare is not None:
            body["include_compare"] = include_compare
        out = self._request(
            "POST", "/v1/code/github/get-batch-branch-status",
            json=body, params={"caller": caller} if caller else None, retry=True)
        return (out.get("branch_statuses") if isinstance(out, dict) else None) or []

    def get_code_repo(self, repo: str) -> dict:
        """``GET /api/oauth/organizations/{org}/code/repos/{owner}/{name}`` (the CLI's
        bearer route, ``surface.md`` §4.1) -> ``{repo: {default_branch, owner,
        name}, status: {app_installed} | null}``.

        The web's repo list (``/api/organizations/{org}/code/repos``) and branch
        list (``/api/github/organizations/{org}/github/{owner}/{repo}/branches``)
        are claude.ai cookie routes (HAR #15/#3); whether they accept a bearer
        is unknown, so they are not wrapped here."""
        owner, _, name = repo_slug(repo).partition("/")
        return self._request(
            "GET", f"/api/oauth/organizations/{self.org_uuid}/code/repos/{owner}/{name}",
            web=False, client_platform=self.client_platform)
