"""Unofficial Python client for the Claude Code Remote Control / Sessions API.

Reverse-engineered from the Claude Code CLI. Not affiliated with or supported by
Anthropic; the underlying endpoints are private and may change without notice.

Quick start (Remote Control — steer a live `claude remote-control` session):

    from claude_rc import RemoteControlClient

    rc = RemoteControlClient()                 # reads ~/.claude credentials
    for s in rc.sessions():
        print(s.get("id"), s.get("title"))

    sid = rc.sessions()[0]["id"]
    for ev in rc.send_and_collect(sid, "run the tests", print_stream=True):
        ...

Claude Code on the web (0.4.0 local additions — cloud sessions end to end):

    rc = RemoteControlClient()     # CLAUDE_AI_ACCESS_TOKEN, cswap slot, cookie, or ~/.claude
    env = default_environment(rc.list_environments())
    s = rc.create_session(env["id"], "hi", model="claude-haiku-5-5", repo="owner/name")
    for p in rc.pending_requests(s["id"]):
        if p.questions:
            rc.select_option(s["id"], p, p.questions[0].options[0].label)
    rc.save_speech("reply.ogg", "Hello from Claude")
"""

from ._version import __version__
from .client import (
    APIError,
    ControlRejected,
    ControlTimeout,
    ManagedAgentsClient,
    RemoteControlClient,
    CCR_BETA,
    CLIENT_FEATURE,
    FILES_BETAS,
    MANAGED_AGENTS_BETA,
    TRIGGERS_BETA,
    build_headers,
)
from .credentials import (
    AuthSource,
    CredentialsError,
    OAuthCredentials,
    cswap_credentials,
    load_credentials,
    load_org_uuid,
    load_slot_token,
    org_uuid_from_profile,
    refresh_credentials,
    resolve_auth,
)
from .environments import (
    bridge_machines,
    cloud_environment_body,
    default_environment,
    environment_items,
    environment_target,
    environments_of_kind,
    redact_environment,
)
from .events import (
    Event,
    RC,
    RCSystem,
    Recv,
    Send,
    StreamFrame,
    cli_user_message,
    cli_control_request,
    cli_control_cancel_request,
    cli_control_response,
    cli_permission_response,
    cli_question_response,
    cli_dialog_response,
    cli_web_user_message,
    cli_permission_decision,
    cli_question_answer,
    cli_user_dialog_response,
    cli_elicitation_response,
    cli_hook_callback_response,
    image_block,
    join_answer_labels,
    user_message,
    interrupt,
    custom_tool_result,
)
from .files import attachment_ref, read_upload, user_message_with_files
from .interaction import (
    PendingRequest,
    Question,
    QuestionOption,
    SlashCommand,
    WatchEvent,
    build_answers,
    find_pending,
    is_client_only,
    pending_from_initialize,
)
from .lifecycle import (
    branch_for,
    initial_events,
    repo_slug,
    session_web_url,
    to_cse_id,
    to_session_id,
)
from .sse import SSEFrame, parse_sse
from .triggers import fire_routine, rfc3339, trigger_body, trigger_prompt, trigger_url
from .tts import TTSError, ogg_duration_ms, ogg_opus, tts_url
from .webauth import CloudflareChallenge, CookieJar, keepalive, load_jar
from .webui import serve as serve_webui

__all__ = [
    "__version__",
    "RemoteControlClient",
    "ManagedAgentsClient",
    "APIError",
    "ControlRejected",
    "ControlTimeout",
    "CCR_BETA",
    "CLIENT_FEATURE",
    "FILES_BETAS",
    "MANAGED_AGENTS_BETA",
    "TRIGGERS_BETA",
    "build_headers",
    # credentials
    "AuthSource",
    "OAuthCredentials",
    "CredentialsError",
    "cswap_credentials",
    "load_credentials",
    "load_org_uuid",
    "load_slot_token",
    "org_uuid_from_profile",
    "refresh_credentials",
    "resolve_auth",
    # environments
    "bridge_machines",
    "cloud_environment_body",
    "default_environment",
    "environment_items",
    "environment_target",
    "environments_of_kind",
    "redact_environment",
    # events
    "Event",
    "RC",
    "RCSystem",
    "Recv",
    "Send",
    "StreamFrame",
    "cli_user_message",
    "cli_control_request",
    "cli_control_cancel_request",
    "cli_control_response",
    "cli_permission_response",
    "cli_question_response",
    "cli_dialog_response",
    "cli_web_user_message",
    "cli_permission_decision",
    "cli_question_answer",
    "cli_user_dialog_response",
    "cli_elicitation_response",
    "cli_hook_callback_response",
    "image_block",
    "join_answer_labels",
    "user_message",
    "interrupt",
    "custom_tool_result",
    # files
    "attachment_ref",
    "read_upload",
    "user_message_with_files",
    # interaction
    "PendingRequest",
    "Question",
    "QuestionOption",
    "SlashCommand",
    "WatchEvent",
    "build_answers",
    "find_pending",
    "is_client_only",
    "pending_from_initialize",
    # lifecycle
    "branch_for",
    "initial_events",
    "repo_slug",
    "session_web_url",
    "to_cse_id",
    "to_session_id",
    # triggers
    "fire_routine",
    "rfc3339",
    "trigger_body",
    "trigger_prompt",
    "trigger_url",
    # tts
    "TTSError",
    "ogg_duration_ms",
    "ogg_opus",
    "tts_url",
    "SSEFrame",
    "parse_sse",
    "serve_webui",
    "CookieJar",
    "CloudflareChallenge",
    "load_jar",
    "keepalive",
]
