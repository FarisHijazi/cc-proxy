"""Interaction: chat, every control request, permission prompts,
AskUserQuestion (single/multi-select, "Other" free text, Skip), slash
commands, live streams and the account-wide watch feed.

Wire sources: ``spec/sessions.md`` §5 (event model, control subtypes and the
cloud worker's forwarding policy, ``all/a_0258802926.js``), §7 (SSE frames),
§4.11 (watch), the HAR of 2026-10-28 (web send/initialize bodies) and the
ducktape notes on the watch's resume token. Shapes not seen on the wire are
marked *unverified* in their docstrings.
"""

from __future__ import annotations

import json
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

import httpx

from .events import (
    RC,
    STREAM_META_FRAMES,
    WORKER_PROMPT_SUBTYPES,
    Event,
    cli_control_cancel_request,
    cli_elicitation_response,
    cli_hook_callback_response,
    cli_permission_decision,
    cli_question_answer,
    cli_user_dialog_response,
    cli_web_user_message,
    image_block,
    stream_meta_event,
)
from .sse import parse_sse

# --- control policy (sessions.md §5.3, cloud routing `all/a_0258802926.js`) --
# apply_flag_settings keys a cloud-hosted (CCR) worker accepts; anything else
# is rejected "keys not available in a cloud-hosted session".
CLOUD_FLAG_SETTINGS = frozenset(
    {"model", "advisorModel", "effortLevel", "ultracode", "fastMode", "viewMode", "alwaysThinkingEnabled"}
)
# The only keys a Remote Control (bridge) worker applies.
RC_FLAG_SETTINGS = frozenset({"effortLevel", "ultracode"})

# Subtypes a cloud worker refuses outright (so a typed method is pointless).
CLOUD_REJECTED_SUBTYPES = frozenset({
    "set_color", "fork_conversation", "rewind_files", "mcp_set_servers", "update_settings",
    "get_memory_dialog", "get_skills_dialog", "get_status", "export_conversation",
    "list_cloud_sessions", "get_cloud_session", "get_cloud_session_transcript",
    "get_chrome_dialog", "get_chrome_browsers", "select_chrome_browser", "get_sandbox_dialog",
    "set_cwd", "claim_session", "add_directory", "register_repo_root", "set_proactivity_level",
})

# Worker capabilities (system/init `capabilities`) for the interrupt variants.
INTERRUPT_CAPABILITIES = {
    "receipt": "interrupt_receipt_v1",
    "send_now": "interrupt_send_now_v1",
    "cancel_queued": "interrupt_cancel_queued_v1",
}

# The account watch (sessions.md §4.11; ducktape "Session watch").
WATCH_PATH = "/v1/code/sessions/watch"
WATCH_PLATFORM = "web_claude_ai"   # any other platform gets 404 "endpoint not enabled"
WATCH_TOKEN_MAX_AGE = 225.0        # a token ≥ 240 s old opens a watch that never delivers
WATCH_SYNC_STALL = 240.0           # sync recurs every 100-120 s; this long without one = dead
WATCH_KEEPALIVE_TOLERANCE = 135    # what the web app asks for (web-capture 2026-10-23)
WATCH_GONE_GRACE = 30.0            # a 410 this soon after minting ends the watch (CLI)
STREAM_LIVENESS = 45.0             # CLI: 45 s with no frame -> reconnect (sessions.md §7)

# Slash commands the web composer lists ahead of the VM's own skills
# (web-capture 2026-10-23, step 8; the popup opened with no request). For each,
# the typed method that does the same, or None for a UI-only one. Which of these
# the web handles itself rather than sending "/name" is inferred, unverified.
WEB_CLIENT_COMMANDS: dict[str, Optional[str]] = {
    "btw": "side_question",
    "rewind": "rewind_conversation",
    "fork": "fork (lifecycle); fork_conversation is rejected in cloud sessions",
    "feedback": "submit_feedback",
    "model": "set_model",
    "effort": "set_effort",
    "fast": "set_fast_mode",
    "plan": "set_permission_mode('plan')",
    "permissions": "list_permission_rules",
    "skills": "list_commands",
    "rename": "rename_session",
    "diff": "get_workspace_diff",
    "tasks": "background_tasks / get_task_output",
    "config": "get_settings",
    "advisor": "set_advisor_model",
    "sandbox": None,
    "theme": None,
    "login": None,
    "help": None,
    "copy": None,
    "channels": None,
}


def is_client_only(name: str) -> bool:
    """True for a slash command the web app handles in the browser with no
    worker equivalent (``theme``, ``login``, ``help`` …; unverified mapping)."""
    name = name.lstrip("/")
    return name in WEB_CLIENT_COMMANDS and WEB_CLIENT_COMMANDS[name] is None


# --- typed views -----------------------------------------------------------
@dataclass
class QuestionOption:
    """One choice of an AskUserQuestion question."""

    label: str
    description: str = ""
    preview: Optional[str] = None


@dataclass
class Question:
    """One AskUserQuestion question: ``question`` (its text, the answer key),
    a short ``header`` chip, ``options`` and ``multi_select``."""

    question: str
    header: str = ""
    options: list[QuestionOption] = field(default_factory=list)
    multi_select: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def labels(self) -> list[str]:
        return [o.label for o in self.options]

    @classmethod
    def from_wire(cls, q: Mapping[str, Any]) -> "Question":
        opts = [
            QuestionOption(str(o.get("label", "")), str(o.get("description") or ""), o.get("preview"))
            for o in (q.get("options") or [])
            if isinstance(o, Mapping)
        ]
        return cls(
            question=str(q.get("question", "")),
            header=str(q.get("header") or ""),
            options=opts,
            multi_select=bool(q.get("multiSelect")),
            raw=dict(q),
        )


@dataclass
class PendingRequest:
    """A worker ``control_request`` still waiting for a controller's answer.

    ``kind`` is ``question`` (AskUserQuestion), ``permission`` (any other
    ``can_use_tool``), ``dialog`` (``request_user_dialog``), ``elicitation`` or
    ``hook_callback``. ``request`` is the inner ``request`` object.
    """

    request_id: str
    subtype: str
    request: dict
    event: Optional[Event] = None

    @property
    def kind(self) -> str:
        if self.subtype == "can_use_tool":
            return "question" if self.tool_name == "AskUserQuestion" else "permission"
        return {"request_user_dialog": "dialog"}.get(self.subtype, self.subtype)

    @property
    def tool_name(self) -> Optional[str]:
        return self.request.get("tool_name")

    @property
    def tool_use_id(self) -> Optional[str]:
        return self.request.get("tool_use_id")

    @property
    def input(self) -> dict:
        return self.request.get("input") or {}

    @property
    def permission_suggestions(self) -> list[dict]:
        """The rule updates "Always allow" applies (``updatedPermissions``)."""
        return list(self.request.get("permission_suggestions") or [])

    @property
    def sequence_num(self) -> Optional[int]:
        return self.event.sequence_num if self.event else None

    @property
    def questions(self) -> list[Question]:
        """The AskUserQuestion questions ([] for any other kind)."""
        if self.kind != "question":
            return []
        return [Question.from_wire(q) for q in self.input.get("questions") or [] if isinstance(q, Mapping)]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], event: Event | None = None) -> Optional["PendingRequest"]:
        """From a ``control_request`` payload (an event's, or an entry of
        initialize's ``pending_*_requests``); None for anything else."""
        req = payload.get("request")
        if payload.get("type") != RC.CONTROL_REQUEST or not isinstance(req, Mapping):
            return None
        return cls(str(payload.get("request_id")), str(req.get("subtype")), dict(req), event)


@dataclass
class SlashCommand:
    """A command or skill from initialize's ``commands`` (HAR 2026-10-28):
    ``{name, description, argumentHint, aliases?, builtin?}``."""

    name: str
    description: str = ""
    argument_hint: str = ""
    aliases: list[str] = field(default_factory=list)
    builtin: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_wire(cls, c: Mapping[str, Any]) -> "SlashCommand":
        return cls(
            name=str(c.get("name", "")),
            description=str(c.get("description") or ""),
            argument_hint=str(c.get("argumentHint") or ""),
            aliases=list(c.get("aliases") or []),
            builtin=bool(c.get("builtin")),
            raw=dict(c),
        )


@dataclass
class WatchEvent:
    """One frame of the account watch. ``kind``: ``added`` / ``changed``
    (``session`` = the session as the list route sends it), ``removed``
    (``session`` = ``{id}``), ``sync`` (``resume_token`` = the next token), or
    ``resync`` — synthesized by :meth:`InteractionMixin.watch` after it had to
    mint a fresh token, so changes in the gap may be missing: re-list."""

    kind: str
    session: dict = field(default_factory=dict)
    resume_token: Optional[str] = None
    raw: Any = None

    @property
    def session_id(self) -> Optional[str]:
        return self.session.get("id")


# --- pure helpers ----------------------------------------------------------
def find_pending(events: Iterable[Event]) -> list[PendingRequest]:
    """Worker prompts in ``events`` (oldest first) not yet answered.

    A prompt is a ``control_request`` from the worker whose subtype is in
    :data:`~claude_rc.events.WORKER_PROMPT_SUBTYPES`; it is settled by any
    ``control_response`` or ``control_cancel_request`` with its ``request_id``
    (answered here, by another controller, or withdrawn by the worker).
    """
    open_: dict[str, PendingRequest] = {}
    for ev in events:
        if ev.type == RC.CONTROL_REQUEST and ev.source != "client" and ev.control_subtype in WORKER_PROMPT_SUBTYPES:
            pr = PendingRequest.from_payload(ev.payload, ev)
            if pr:
                open_[pr.request_id] = pr
        elif ev.type == RC.CONTROL_RESPONSE and ev.request_id:
            open_.pop(ev.request_id, None)
        elif ev.type == RC.CONTROL_CANCEL_REQUEST:
            open_.pop(str(ev.payload.get("request_id")), None)
    return list(open_.values())


def build_answers(
    questions: Sequence[Question],
    selections: Mapping[Any, str | Sequence[str]] | None = None,
    *,
    other: Mapping[Any, str] | None = None,
) -> dict[str, str | list[str]]:
    """Validate choices and build the ``answers`` map (question text -> label,
    or a list of labels for ``multiSelect``).

    Keys of ``selections`` / ``other`` are a question's text, its ``header`` or
    its index. ``selections`` values must be option labels (one for a
    single-select question; a str or list for multi-select). ``other`` is the
    "Other" free text: the whole answer of a single-select question, or one more
    entry after a multi-select's labels.
    """
    def resolve(key: Any) -> Question:
        return _question(questions, key)

    answers: dict[str, str | list[str]] = {}
    for key, choice in (selections or {}).items():
        q = resolve(key)
        picked = [choice] if isinstance(choice, str) else list(choice)
        unknown = [p for p in picked if p not in q.labels]
        if unknown:
            raise ValueError(f"{unknown!r} not among the options of {q.question!r}: {q.labels!r}")
        if q.multi_select:
            answers[q.question] = picked
        elif len(picked) != 1:
            raise ValueError(f"{q.question!r} is single-select; pick exactly one label")
        else:
            answers[q.question] = picked[0]
    for key, text in (other or {}).items():
        q = resolve(key)
        if q.multi_select:
            prior = answers.get(q.question) or []
            answers[q.question] = [*prior, text] if isinstance(prior, list) else [prior, text]
        else:
            if q.question in answers:
                raise ValueError(f"{q.question!r} is single-select; give a label or Other text, not both")
            answers[q.question] = text
    return answers


def _question(questions: Sequence[Question], key: Any) -> Question:
    """A question by index, text or header (KeyError if none)."""
    if isinstance(key, int) and not isinstance(key, bool) and 0 <= key < len(questions):
        return questions[key]
    for q in questions:
        if key and key in (q.question, q.header):
            return q
    raise KeyError(f"no question {key!r}")


def _as_request(request: PendingRequest | str) -> tuple[str, Optional[PendingRequest]]:
    if isinstance(request, PendingRequest):
        return request.request_id, request
    return str(request), None


class InteractionMixin:
    """Chat, control, prompt and stream methods for :class:`~claude_rc.client.RemoteControlClient`.

    The host (:class:`~claude_rc.client.RemoteControlClient`) provides, and these
    methods must use instead of any HTTP of their own:

    * ``self._request(method, path, *, json=, params=, betas=, client_platform=,
      org=, web=, headers=, accept=, retry=, timeout=, content=, data=, files=)``
      -> parsed body; ``self._send(...)`` -> checked :class:`httpx.Response`
      (``stream=True`` leaves it open). Both raise :class:`~claude_rc.client.APIError`.
    * ``self._path(*parts)`` -> ``/v1/code/sessions/...``; ``self.org_uuid``.
    * ``self.send_events``, ``self._send_control``, ``self._control``,
      ``self.request_control``, ``self.initialize``, ``self.list_events``,
      ``self.list_sessions``.

    A method defined on ``RemoteControlClient`` itself shadows a mixin method of
    the same name, so none here reuses one of its names. Control methods return
    the worker's ``response`` object and raise
    :class:`~claude_rc.client.ControlRejected` /
    :class:`~claude_rc.client.ControlTimeout`; RC/CCR notes are per
    sessions.md §5.3 ("CCR only" = a Remote Control bridge refuses it).
    """

    # -- chat ---------------------------------------------------------------
    def send_prompt(
        self,
        session_id: str,
        text: str = "",
        *,
        images: Sequence[tuple[bytes | str, str] | dict] = (),
        file_attachments: Sequence[Mapping[str, Any]] = (),
        priority: str | None = None,
        requeue_if_interrupted_within_ms: int | None = None,
        message_uuid: str | None = None,
        **extra: Any,
    ) -> dict:
        """Send a user message the way claude.ai/code does (plain-string
        ``content``, ``session_id`` in the event). Returns the events ack.

        * ``images``: ``(data, media_type)`` pairs or ready ``image`` blocks;
          they turn ``content`` into ``[text, image…]`` blocks.
        * ``file_attachments``: refs from ``upload_attachment``
          (``{file_uuid, file_name, is_image?, sha256?, file_size?}``).
        * ``priority``: ``now`` | ``next`` | ``later`` (``later`` = Ctrl+Enter
          "queue for later" while a turn runs).
        * ``requeue_if_interrupted_within_ms`` (≤ 10000): put the message back
          in the queue if the turn it starts is interrupted that soon.
        * ``message_uuid``: keep it to cancel (:meth:`cancel_queued_message`)
          or :meth:`send_queued_now`.
        * ``extra``: other user-message fields verbatim (``shouldQuery``,
          ``client_composed`` …; sessions.md §5.2).
        """
        content: str | list = text
        if images:
            blocks: list[dict] = [{"type": "text", "text": text}] if text else []
            for img in images:
                blocks.append(dict(img) if isinstance(img, Mapping) else image_block(*img))
            content = blocks
        event = cli_web_user_message(
            content,
            session_id=session_id,
            message_uuid=message_uuid,
            priority=priority,
            requeue_if_interrupted_within_ms=requeue_if_interrupted_within_ms,
            file_attachments=[dict(a) for a in file_attachments] or None,
            **extra,
        )
        return self.send_events(session_id, [event])

    def queue_message(self, session_id: str, text: str, *, message_uuid: str | None = None, **kw: Any) -> dict:
        """"Queue for later": a ``priority: later`` message that waits for the
        running turn instead of joining it."""
        return self.send_prompt(session_id, text, priority="later", message_uuid=message_uuid, **kw)

    # -- interrupt variants (sessions.md §5.3) -------------------------------
    def interrupt_turn(
        self,
        session_id: str,
        *,
        reason: str | None = None,
        cancel_queued: bool = False,
        send_now: bool = False,
        message_uuid: str | None = None,
        for_user_message_uuids: Sequence[str] | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Send an ``interrupt`` control with any of its fields.

        Without ``timeout`` it fires and returns the events ack. With one it
        waits for the receipt (workers with ``interrupt_receipt_v1``):
        ``{still_queued[], put_back?, send_now?: stopped|delivering|nothing_waiting,
        turn?: stopped|idle|spared, cancelled?}``. A Remote Control worker acks
        with an empty response.
        """
        fields: dict[str, Any] = {}
        if reason is not None:
            fields["reason"] = reason
        if cancel_queued:
            fields["cancel_queued"] = True
        if send_now:
            fields["send_now"] = True
        if message_uuid is not None:
            fields["message_uuid"] = message_uuid
        if for_user_message_uuids:
            if not 1 <= len(for_user_message_uuids) <= 8:
                raise ValueError("for_user_message_uuids takes 1-8 uuids")
            fields["for_user_message_uuids"] = list(for_user_message_uuids)
        if timeout is None:
            return self._send_control(session_id, "interrupt", **fields)[1]
        return self._control(session_id, "interrupt", timeout, **fields)[1].get("response") or {}

    def stop_turn(self, session_id: str, *, timeout: float | None = None) -> dict:
        """The stop button: ``{subtype: "interrupt"}``."""
        return self.interrupt_turn(session_id, timeout=timeout)

    def cancel_queued(self, session_id: str, *, timeout: float | None = None) -> dict:
        """Interrupt and drop queued messages (the CLI's legacy
        ``{interrupt, cancel_queued: true}``)."""
        return self.interrupt_turn(session_id, cancel_queued=True, timeout=timeout)

    def send_queued_now(self, session_id: str, message_uuid: str | None = None, *, timeout: float | None = None) -> dict:
        """"Send now": stop the turn and deliver a queued message
        (``{interrupt, send_now: true, message_uuid?}``)."""
        return self.interrupt_turn(session_id, send_now=True, message_uuid=message_uuid, timeout=timeout)

    def cancel_control(self, session_id: str, request_id: str) -> dict:
        """Withdraw one of your own in-flight control requests (no reply)."""
        return self.send_events(session_id, [cli_control_cancel_request(request_id)])

    # -- settings controls ---------------------------------------------------
    def set_max_thinking_tokens(
        self,
        session_id: str,
        max_thinking_tokens: int | None,
        *,
        thinking_display: str | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """Thinking budget (``None`` resets to the session default);
        ``thinking_display`` ∈ ``summarized`` | ``omitted`` | ``highlights``."""
        fields: dict[str, Any] = {"max_thinking_tokens": max_thinking_tokens}
        if thinking_display is not None:
            fields["thinking_display"] = thinking_display
        return self.request_control(session_id, "set_max_thinking_tokens", timeout=timeout, **fields)

    def apply_flag_settings(
        self,
        session_id: str,
        settings: Mapping[str, Any],
        *,
        allowed: frozenset[str] | None = CLOUD_FLAG_SETTINGS,
        timeout: float = 15.0,
    ) -> dict:
        """Merge ``settings`` into the worker's flag layer (session-scoped).

        A cloud worker takes only :data:`CLOUD_FLAG_SETTINGS`; a Remote Control
        bridge only :data:`RC_FLAG_SETTINGS` — pass ``allowed=RC_FLAG_SETTINGS``
        for one, or ``None`` to skip the local check. Other keys raise
        ValueError before anything is sent.
        """
        if allowed is not None:
            bad = sorted(set(settings) - allowed)
            if bad:
                raise ValueError(f"apply_flag_settings keys not accepted: {bad}")
        return self.request_control(session_id, "apply_flag_settings", timeout=timeout, settings=dict(settings))

    def set_fast_mode(self, session_id: str, enabled: bool, *, timeout: float = 15.0) -> dict:
        """``/fast``: ``apply_flag_settings {fastMode}`` (CCR only)."""
        return self.apply_flag_settings(session_id, {"fastMode": enabled}, timeout=timeout)

    def set_advisor_model(self, session_id: str, model: str | None, *, timeout: float = 15.0) -> dict:
        """``/advisor``: ``apply_flag_settings {advisorModel}`` (CCR only)."""
        return self.apply_flag_settings(session_id, {"advisorModel": model}, timeout=timeout)

    def set_always_thinking(self, session_id: str, enabled: bool, *, timeout: float = 15.0) -> dict:
        """``apply_flag_settings {alwaysThinkingEnabled}`` (CCR only)."""
        return self.apply_flag_settings(session_id, {"alwaysThinkingEnabled": enabled}, timeout=timeout)

    def set_view_mode(self, session_id: str, mode: str, *, timeout: float = 15.0) -> dict:
        """``apply_flag_settings {viewMode}`` (CCR only). The accepted values are
        unverified (the web's Transcript view offers Normal / Verbose)."""
        return self.apply_flag_settings(session_id, {"viewMode": mode}, timeout=timeout)

    def set_worker_title(
        self, session_id: str, title: str, *, source: str = "remote", timeout: float = 15.0
    ) -> dict:
        """``rename_session`` control: retitle through the worker (``source`` ∈
        ``remote`` | ``host``). To retitle in every list use ``rename_session``
        (``PUT``)."""
        return self.request_control(session_id, "rename_session", timeout=timeout, title=title, source=source)

    def generate_session_title(
        self, session_id: str, description: str, *, persist: bool | None = None, timeout: float = 60.0
    ) -> Optional[str]:
        """Have the worker title a description (``{description, persist?}`` ->
        ``response.title``, the SDK's ``generateSessionTitle``). CCR only."""
        fields: dict[str, Any] = {"description": description}
        if persist is not None:
            fields["persist"] = persist
        return self.request_control(session_id, "generate_session_title", timeout=timeout, **fields).get("title")

    def end_session(self, session_id: str, reason: str | None = None, *, wait: float | None = None) -> dict:
        """Ask a cloud worker to shut down (``end_session {reason}``, handled by
        the host relay). Fires and returns the ack unless ``wait`` is given."""
        fields = {"reason": reason} if reason is not None else {}
        if wait is None:
            return self._send_control(session_id, "end_session", **fields)[1]
        return self.request_control(session_id, "end_session", timeout=wait, **fields)

    def rewind_conversation(
        self,
        session_id: str,
        target_message_uuid: str,
        *,
        last_seen_user_message_uuid: str | None = None,
        allow_assistant_target: bool | None = None,
        allow_summarized_target: bool | None = None,
        expires_at: int | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """``/rewind`` to a user message (CCR only). Returns ``{rewound,
        targetMessageUuid, prefillText, precedingAssistantUuid, resolution?}``
        (fields read from the worker's ``print.ts``; ``expires_at`` epoch ms)."""
        fields: dict[str, Any] = {"target_message_uuid": target_message_uuid}
        for key, value in (
            ("last_seen_user_message_uuid", last_seen_user_message_uuid),
            ("allow_assistant_target", allow_assistant_target),
            ("allow_summarized_target", allow_summarized_target),
            ("expires_at", expires_at),
        ):
            if value is not None:
                fields[key] = value
        return self.request_control(session_id, "rewind_conversation", timeout=timeout, **fields)

    def set_prompt_suggestions_paused(self, session_id: str, paused: bool, *, timeout: float = 15.0) -> dict:
        return self.request_control(session_id, "set_prompt_suggestions_paused", timeout=timeout, paused=paused)

    # -- MCP / plugins / skills ----------------------------------------------
    def mcp_status(self, session_id: str, *, live_reply: bool | None = None, timeout: float = 15.0) -> list[dict]:
        """``mcpServers`` (``[]`` on a Remote Control bridge). ``live_reply``
        answers on the ephemeral channel only — leave it unset here, since
        :meth:`request_control` polls history."""
        fields = {"liveReply": live_reply} if live_reply is not None else {}
        return list(self.request_control(session_id, "mcp_status", timeout=timeout, **fields).get("mcpServers") or [])

    def mcp_toggle(self, session_id: str, server_name: str, enabled: bool, *, timeout: float = 30.0) -> dict:
        """Enable/disable an MCP server (CCR only)."""
        return self.request_control(session_id, "mcp_toggle", timeout=timeout, serverName=server_name, enabled=enabled)

    def mcp_reconnect(self, session_id: str, server_name: str, *, timeout: float = 30.0) -> dict:
        return self.request_control(session_id, "mcp_reconnect", timeout=timeout, serverName=server_name)

    def set_mcp_permission_mode_override(
        self,
        session_id: str,
        server_name: str,
        mode: str | None,
        *,
        ask_for_tools: Sequence[str] | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """Pin (``default`` | ``auto``) or clear (None) a server's permission mode."""
        fields: dict[str, Any] = {"serverName": server_name, "mode": mode}
        if ask_for_tools is not None:
            fields["askForTools"] = list(ask_for_tools)
        return self.request_control(session_id, "set_mcp_permission_mode_override", timeout=timeout, **fields)

    def mcp_call(
        self,
        session_id: str,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_ms: int | None = None,
        timeout: float = 60.0,
    ) -> dict:
        """Invoke an MCP tool without a model turn (no permission check; CCR
        only). ``tool`` is the full tool name; result shape unverified."""
        fields: dict[str, Any] = {"tool": tool, "arguments": dict(arguments or {})}
        if timeout_ms is not None:
            fields["timeout_ms"] = timeout_ms
        return self.request_control(session_id, "mcp_call", timeout=timeout, **fields)

    def reload_skills(self, session_id: str, *, timeout: float = 30.0) -> dict:
        return self.request_control(session_id, "reload_skills", timeout=timeout)

    def reload_plugins(self, session_id: str, *, hold_on_cache_impact: bool = False, timeout: float = 60.0) -> dict:
        """Refresh plugins (``add_plugins`` is refused in cloud sessions)."""
        fields = {"hold_on_cache_impact": True} if hold_on_cache_impact else {}
        return self.request_control(session_id, "reload_plugins", timeout=timeout, **fields)

    def reload_output_styles(self, session_id: str, *, timeout: float = 30.0) -> dict:
        return self.request_control(session_id, "reload_output_styles", timeout=timeout)

    # -- read-only queries (CCR) -----------------------------------------------
    def get_session_cost(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """The ``/usage`` cost text the CLI prints in non-interactive mode."""
        return self.request_control(session_id, "get_session_cost", timeout=timeout)

    def list_models(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """The worker's selectable model catalog (shape unverified)."""
        return self.request_control(session_id, "list_models", timeout=timeout)

    def get_binary_version(self, session_id: str, *, timeout: float = 15.0) -> dict:
        return self.request_control(session_id, "get_binary_version", timeout=timeout)

    def get_settings(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """Effective merged settings plus the raw per-source ones."""
        return self.request_control(session_id, "get_settings", timeout=timeout)

    def get_hooks_listing(self, session_id: str, *, timeout: float = 15.0) -> dict:
        return self.request_control(session_id, "get_hooks_listing", timeout=timeout)

    def list_permission_rules(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """Live permission rules and workspace directories (``/permissions``)."""
        return self.request_control(session_id, "list_permission_rules", timeout=timeout)

    def get_plan(self, session_id: str, *, timeout: float = 15.0) -> dict:
        """The current plan-mode plan (the worker resolves its own plan file)."""
        return self.request_control(session_id, "get_plan", timeout=timeout)

    def read_file(
        self,
        session_id: str,
        path: str,
        *,
        max_bytes: int | None = None,
        encoding: str | None = None,
        anchor: str | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """Read a file in the session (content + ``absPath``), relative to cwd,
        gated by the worker's read rules."""
        fields: dict[str, Any] = {"path": path}
        for key, value in (("max_bytes", max_bytes), ("encoding", encoding), ("anchor", anchor)):
            if value is not None:
                fields[key] = value
        return self.request_control(session_id, "read_file", timeout=timeout, **fields)

    def list_directory(self, session_id: str, path: str = "", *, timeout: float = 15.0) -> dict:
        """Direct children (names and kinds) of one directory under the
        workspace root (CCR only)."""
        return self.request_control(session_id, "list_directory", timeout=timeout, path=path)

    def file_suggestions(self, session_id: str, query: str, *, timeout: float = 15.0) -> list[str]:
        """``@``-mention autocomplete: matching paths."""
        resp = self.request_control(session_id, "file_suggestions", timeout=timeout, query=query)
        return [s.get("path") if isinstance(s, Mapping) else s for s in resp.get("suggestions") or []]

    def seed_read_state(self, session_id: str, path: str, mtime: float, *, timeout: float = 15.0) -> dict:
        return self.request_control(session_id, "seed_read_state", timeout=timeout, path=path, mtime=mtime)

    # -- tasks -----------------------------------------------------------------
    def background_tasks(self, session_id: str, tool_use_id: str | None = None, *, timeout: float = 15.0) -> dict:
        """Background running foreground tasks (one, by ``tool_use_id``) —
        Ctrl+B. Returns ``{backgrounded?}``."""
        fields = {"tool_use_id": tool_use_id} if tool_use_id else {}
        return self.request_control(session_id, "background_tasks", timeout=timeout, **fields)

    def get_task_output(self, session_id: str, task_id: str, *, timeout: float = 15.0) -> dict:
        """Tail (≤ 8 KiB) of a background shell/Monitor task:
        ``{output, total_bytes, truncated}``."""
        return self.request_control(session_id, "get_task_output", timeout=timeout, task_id=task_id)

    def send_task_message(self, session_id: str, task_id: str, message: str, *, timeout: float = 15.0) -> dict:
        """Message a running agent task. Only ``task_id`` is confirmed in the
        CLI; the ``message`` field name is unverified."""
        return self.request_control(session_id, "send_task_message", timeout=timeout, task_id=task_id, message=message)

    def start_shell_task(
        self,
        session_id: str,
        command: str,
        *,
        args: Sequence[str] | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_ms: int | None = None,
        description: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Start a background shell task, e.g. a dev server (cloud workers with
        ``start_shell_task_v1``)."""
        fields: dict[str, Any] = {"command": command}
        for key, value in (("args", list(args) if args is not None else None), ("cwd", cwd),
                           ("env", dict(env) if env is not None else None), ("timeout_ms", timeout_ms),
                           ("description", description)):
            if value is not None:
                fields[key] = value
        return self.request_control(session_id, "start_shell_task", timeout=timeout, **fields)

    # -- feedback ----------------------------------------------------------------
    def submit_feedback(
        self, session_id: str, description: str, *, surface: str = "ccr", timeout: float = 30.0, **fields: Any
    ) -> dict:
        """``/feedback`` (``surface`` ∈ cli|ccd|ccr|ide|sdk|cowork; extra fields:
        ``title``, ``type``, ``area``, ``attach_transcript`` …)."""
        return self.request_control(
            session_id, "submit_feedback", timeout=timeout, description=description, surface=surface, **fields
        )

    def message_rated(
        self,
        session_id: str,
        message_uuid: str,
        sentiment: str,
        *,
        surface: str | None = None,
        cleared: bool = False,
        timeout: float = 15.0,
    ) -> dict:
        """Thumbs up/down on one message (``sentiment`` values unverified)."""
        fields: dict[str, Any] = {"messageUuid": message_uuid, "sentiment": sentiment, "cleared": cleared}
        if surface is not None:
            fields["surface"] = surface
        return self.request_control(session_id, "message_rated", timeout=timeout, **fields)

    # -- answering worker prompts ------------------------------------------------
    def allow_once(
        self, session_id: str, request: PendingRequest | str, *, updated_input: dict | None = None
    ) -> dict:
        """"Allow once": allow this call only. ``updated_input`` defaults to the
        request's own input (older workers expect ``updatedInput``)."""
        rid, pr = _as_request(request)
        answer = cli_permission_decision(
            rid,
            allow=True,
            updated_input=updated_input if updated_input is not None else (pr.input if pr else None),
            tool_use_id=pr.tool_use_id if pr else None,
            tool_name=pr.tool_name if pr else None,
            classification="user_temporary",
        )
        return self.send_events(session_id, [answer])

    def always_allow(
        self,
        session_id: str,
        request: PendingRequest | str,
        *,
        permissions: Sequence[Mapping[str, Any]] | None = None,
        updated_input: dict | None = None,
    ) -> dict:
        """"Always allow": allow and apply rule updates — ``permissions``, else
        the request's ``permission_suggestions`` (the web says these last for
        the session). Raises ValueError when there are none to apply."""
        rid, pr = _as_request(request)
        rules = [dict(p) for p in permissions] if permissions is not None else (pr.permission_suggestions if pr else [])
        if not rules:
            raise ValueError("always_allow needs permission rules: pass permissions= or a PendingRequest with suggestions")
        answer = cli_permission_decision(
            rid,
            allow=True,
            updated_input=updated_input if updated_input is not None else (pr.input if pr else None),
            updated_permissions=rules,
            tool_use_id=pr.tool_use_id if pr else None,
            tool_name=pr.tool_name if pr else None,
            classification="user_permanent",
        )
        return self.send_events(session_id, [answer])

    def deny_permission(
        self,
        session_id: str,
        request: PendingRequest | str,
        message: str = "The user denied this action.",
        *,
        interrupt: bool | None = None,
    ) -> dict:
        """Deny; ``message`` is what the model is told, ``interrupt=True`` also
        stops the turn."""
        rid, pr = _as_request(request)
        answer = cli_permission_decision(
            rid, allow=False, message=message, interrupt=interrupt,
            tool_use_id=pr.tool_use_id if pr else None, classification="user_reject",
        )
        return self.send_events(session_id, [answer])

    def answer_dialog(
        self, session_id: str, request: PendingRequest | str, *, completed: bool = True, result: Any = None
    ) -> dict:
        """Settle a ``request_user_dialog`` (``behavior: completed | cancelled``).
        Only answer kinds you declared in ``supportedDialogKinds``."""
        rid, _ = _as_request(request)
        return self.send_events(session_id, [cli_user_dialog_response(rid, completed=completed, result=result)])

    def answer_elicitation(
        self, session_id: str, request: PendingRequest | str, action: str = "accept", content: dict | None = None
    ) -> dict:
        """Answer an MCP ``elicitation``: ``accept`` (with form ``content``),
        ``decline`` or ``cancel``."""
        rid, _ = _as_request(request)
        return self.send_events(session_id, [cli_elicitation_response(rid, action, content)])

    def answer_hook_callback(self, session_id: str, request: PendingRequest | str, output: Mapping[str, Any]) -> dict:
        """Answer a ``hook_callback`` with the hook's JSON output."""
        rid, _ = _as_request(request)
        return self.send_events(session_id, [cli_hook_callback_response(rid, dict(output))])

    # -- AskUserQuestion -----------------------------------------------------------
    def answer_questions(
        self,
        session_id: str,
        request: PendingRequest,
        selections: Mapping[Any, str | Sequence[str]] | None = None,
        *,
        other: Mapping[Any, str] | None = None,
        skipped: Sequence[Any] = (),
        notes: Mapping[Any, str] | None = None,
    ) -> dict:
        """Answer an AskUserQuestion card (see :func:`build_answers` for
        ``selections`` / ``other`` keys and checks). ``skipped`` questions get
        no answer and ``annotations[q].unanswered = "skipped"``; ``notes`` adds
        ``annotations[q].notes``. Encoding per :func:`~claude_rc.events.cli_question_answer`."""
        if request.kind != "question":
            raise ValueError(f"request {request.request_id} is a {request.kind}, not an AskUserQuestion")
        qs = request.questions
        answers = build_answers(qs, selections, other=other)
        annotations: dict[str, dict] = {}
        for key in skipped:
            annotations.setdefault(_question(qs, key).question, {})["unanswered"] = "skipped"
        for key, text in (notes or {}).items():
            annotations.setdefault(_question(qs, key).question, {})["notes"] = text
        answer = cli_question_answer(
            request.request_id, request.input, answers, annotations=annotations or None, tool_use_id=request.tool_use_id
        )
        return self.send_events(session_id, [answer])

    def select_option(self, session_id: str, request: PendingRequest, label: str, *, question: Any = 0) -> dict:
        """Single-select: pick ``label`` for ``question`` (index, text or header)."""
        return self.answer_questions(session_id, request, {question: label})

    def select_options(
        self, session_id: str, request: PendingRequest, labels: Sequence[str], *, question: Any = 0, other: str | None = None
    ) -> dict:
        """Multi-select: pick ``labels`` (plus optional "Other" text)."""
        return self.answer_questions(
            session_id, request, {question: list(labels)}, other={question: other} if other else None
        )

    def answer_other(self, session_id: str, request: PendingRequest, text: str, *, question: Any = 0) -> dict:
        """"Other": answer ``question`` with free text."""
        return self.answer_questions(session_id, request, other={question: text})

    def skip_questions(self, session_id: str, request: PendingRequest) -> dict:
        """Skip the card: no answers, every question annotated ``skipped`` (the
        worker reads empty answers as "the user did not answer"). That the web's
        Skip button sends exactly this is unverified."""
        return self.answer_questions(session_id, request, skipped=list(range(len(request.questions))))

    # -- pending prompts -------------------------------------------------------------
    def pending_requests(self, session_id: str, *, limit: int = 200) -> list[PendingRequest]:
        """Unanswered worker prompts (permission, AskUserQuestion, dialog,
        elicitation, hook_callback) among the newest ``limit`` events, oldest
        first. ``initialize()``'s ``pending_*_requests`` is the worker's own
        view (see :func:`pending_from_initialize`)."""
        page = self.list_events(session_id, limit=limit, sort_order="desc")
        return find_pending(sorted(page, key=lambda e: e.sequence_num or 0))

    # -- slash commands ----------------------------------------------------------------
    def list_commands(self, session_id: str, *, timeout: float = 15.0) -> list[SlashCommand]:
        """Commands and skills the worker offers, from ``initialize``'s
        ``commands`` (the web composer prepends :data:`WEB_CLIENT_COMMANDS`)."""
        init = self.initialize(session_id, timeout=timeout)
        return [SlashCommand.from_wire(c) for c in init.get("commands") or [] if isinstance(c, Mapping)]

    def run_command(self, session_id: str, name: str, args: str = "", *, strict: bool = False, **kw: Any) -> dict:
        """Run a slash command: a user message ``"/name args"``, which the worker
        dispatches (unless sent ``client_composed``). ``strict`` raises
        ValueError for a web UI-only command (:func:`is_client_only`)."""
        name = name.lstrip("/")
        if strict and is_client_only(name):
            raise ValueError(f"/{name} is handled by the web UI, not the worker")
        text = f"/{name} {args}".rstrip() if args else f"/{name}"
        return self.send_prompt(session_id, text, **kw)

    # -- streams -----------------------------------------------------------------------
    def stream_session(
        self,
        session_id: str,
        *,
        from_sequence_num: int = 0,
        control_only: bool = False,
        reconnect: bool = True,
        max_reconnects: int = 20,
        liveness: float = STREAM_LIVENESS,
    ) -> Iterator[Event]:
        """``GET …/events/stream`` yielding every frame as an :class:`Event`.

        Like ``stream_events`` (resume via ``from_sequence_num`` +
        ``Last-Event-ID``, de-dupe by ``sequence_num``) but ``control_only=True``
        adds ``control_only=1`` (control traffic only), and the envelope-less
        frames come through instead of being dropped: ``session_update``,
        ``security_update``, ``delivery_update`` and ``catch_up_truncated``
        arrive as events whose ``type`` is the frame name (see
        :data:`~claude_rc.events.StreamFrame`). No frame for ``liveness``
        seconds counts as a dead connection and reconnects.
        """
        seen: set[int] = set()
        last_seq = from_sequence_num
        attempts = 0
        while True:
            params: dict[str, Any] = {"from_sequence_num": last_seq}
            if control_only:
                params["control_only"] = "1"
            headers = {"Accept": "text/event-stream"}
            if last_seq:
                headers["Last-Event-ID"] = str(last_seq)
            try:
                with closing(self._send(
                    "GET", self._path(session_id, "events", "stream"), headers=headers, params=params,
                    accept=(200,), retry=False, stream=True, timeout=httpx.Timeout(30.0, read=liveness),
                )) as resp:
                    attempts = 0
                    for frame in parse_sse(resp.iter_lines()):
                        if frame.is_heartbeat:
                            continue
                        if frame.id:
                            try:
                                last_seq = max(last_seq, int(frame.id))
                            except ValueError:
                                pass
                        try:
                            data = json.loads(frame.data) if frame.data else {}
                        except json.JSONDecodeError:
                            continue
                        if frame.event in STREAM_META_FRAMES:
                            yield stream_meta_event(frame.event, data)
                            continue
                        if not isinstance(data, dict):
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
                time.sleep(min(2.0 ** attempts, 30.0))
                continue
            if not reconnect:
                return

    def stream_control(self, session_id: str, **kw: Any) -> Iterator[Event]:
        """:meth:`stream_session` with ``control_only=True``: prompts, verdicts
        and meta frames, without the transcript."""
        return self.stream_session(session_id, control_only=True, **kw)

    def mint_resume_token(self) -> str:
        """A fresh watch resume token: ``GET /v1/code/sessions?limit=1`` ->
        ``resume_token`` (it goes stale in about four minutes)."""
        body = self.list_sessions(limit=1)
        token = body.get("resume_token") if isinstance(body, Mapping) else None
        if not token:
            raise RuntimeError("session list returned no resume_token")
        return str(token)

    def watch(
        self,
        *,
        resume_token: str | None = None,
        include_trigger_sessions: bool = True,
        exclude_tags: str = "-",
        channels_changed: bool | None = None,
        keepalive_tolerance_seconds: int | None = WATCH_KEEPALIVE_TOLERANCE,
        reconnect: bool = True,
        max_reconnects: int = 20,
    ) -> Iterator[WatchEvent]:
        """``GET /v1/code/sessions/watch`` — the account-wide session feed the
        web sidebar follows. Yields :class:`WatchEvent`.

        Sent with ``anthropic-client-platform: web_claude_ai`` (anything else is
        404 "endpoint not enabled"), the token in both ``resume_token`` and
        ``Last-Event-ID``. On connect the server sends ``added`` per live session
        and a ``sync`` whose id is the next token, then ``changed`` /
        ``removed``. A token ≥ 240 s old gets a 200 that never delivers, so this
        re-mints one older than :data:`WATCH_TOKEN_MAX_AGE` before reconnecting,
        and drops a connection that went :data:`WATCH_SYNC_STALL` seconds
        without a ``sync`` — each re-mint after the first yields a ``resync``
        event (re-list to cover the gap). A caller-supplied ``resume_token`` is
        assumed fresh. 410 (token expired) re-mints, unless it came within
        :data:`WATCH_GONE_GRACE` s of minting, which ends the watch.
        """
        from .client import APIError  # deferred: client imports this module

        token, token_at, minted_at = resume_token, time.monotonic(), None
        connected_once = False
        attempts = 0
        while True:
            if token is None or time.monotonic() - token_at >= WATCH_TOKEN_MAX_AGE:
                token, token_at = self.mint_resume_token(), time.monotonic()
                minted_at = token_at
                if connected_once:
                    yield WatchEvent("resync", resume_token=token)
            params: dict[str, Any] = {"exclude_tags": exclude_tags, "resume_token": token}
            if include_trigger_sessions:
                params["include_trigger_sessions"] = "true"
            if channels_changed is not None:
                params["channels_changed"] = str(channels_changed).lower()
            if keepalive_tolerance_seconds is not None:
                params["keepalive_tolerance_seconds"] = keepalive_tolerance_seconds
            read = float(keepalive_tolerance_seconds or 120) + 30.0
            try:
                with closing(self._send(
                    "GET", WATCH_PATH, params=params, client_platform=WATCH_PLATFORM,
                    headers={"Accept": "text/event-stream", "Last-Event-ID": token},
                    accept=(200,), retry=False, stream=True, timeout=httpx.Timeout(30.0, read=read),
                )) as resp:
                    connected_once, attempts = True, 0
                    last_sync = time.monotonic()
                    for frame in parse_sse(resp.iter_lines()):
                        if frame.event == "sync":
                            last_sync = time.monotonic()
                            if frame.id:
                                token, token_at = frame.id, last_sync
                            yield WatchEvent("sync", resume_token=frame.id, raw=frame.data)
                        elif frame.event:
                            try:
                                data = json.loads(frame.data) if frame.data else {}
                            except json.JSONDecodeError:
                                data = {}
                            session = data if isinstance(data, dict) else {}
                            yield WatchEvent(frame.event, session=session, raw=data)
                        if time.monotonic() - last_sync >= WATCH_SYNC_STALL:
                            token = None  # the token went stale under us: re-mint
                            break
            except APIError as exc:
                if exc.status != 410:
                    raise
                if minted_at is not None and time.monotonic() - minted_at < WATCH_GONE_GRACE:
                    return
                token = None
                continue
            except httpx.HTTPError:
                if not reconnect or attempts >= max_reconnects:
                    raise
                attempts += 1
                time.sleep(min(2.0 ** attempts, 30.0))
                continue
            if not reconnect:
                return


def pending_from_initialize(init: Mapping[str, Any]) -> list[PendingRequest]:
    """The prompts a worker reports as outstanding in an ``initialize`` answer
    (``pending_permission_requests`` + ``pending_user_dialog_requests``, each a
    full ``control_request`` frame)."""
    out = []
    for key in ("pending_permission_requests", "pending_user_dialog_requests"):
        for payload in init.get(key) or []:
            pr = PendingRequest.from_payload(payload) if isinstance(payload, Mapping) else None
            if pr:
                out.append(pr)
    return out
