# Claude Code on the web: a Python client (`python/claude_rc`)

`python/claude_rc` is a controller client for claude.ai/code. It does what the
web app does: create, list, steer, answer, archive and delete cloud sessions,
plus environments, scheduled tasks, uploads and claude.ai's text to speech. It
ships in the `cc-proxy` wheel as the `claude-rc` command. Nothing in it has run
against the live service yet. The tests use `httpx.MockTransport` and a fake
websocket server. "Verified offline" below means checked against the specs and
HAR. Real traffic has not confirmed it yet.

Evidence, kept in the session scratchpad and not in the repo:

- `spec/sessions.md`: the session protocol, read out of the Claude Code CLI 2.1.312 chunks.
- `spec/surface.md`: auth, environments, triggers, GitHub and the `/v1/code/*` inventory.
- `spec/har-2026-10-28.md`: a sanitized HAR of the web client. It covers a new session on
  this repo with Opus 5.5 and a "fully open network" environment, the message "hi this is
  a test", one TTS playback, and pin/unpin.
- `spec/web-capture-2026-10-23.md`: an earlier UI walkthrough, with URLs only.

## What already existed (survey)

| Project | Verdict |
| --- | --- |
| `ThatCrispyToast/claude-rc-api` 0.3.0 (MIT) | **Vendored** as `python/claude_rc` (see its `UPSTREAM.md`). It was a Remote Control controller on `/v1/code/sessions`. Here it is bumped to 0.4.0 locally and extended to the whole web surface. |
| `jwoglom/ccweb` | **Has no licence, so it is reference only and nothing was copied.** It is a TUI over the legacy `/v1/sessions` family and replies through `claude -p --cloud`. Its v1 `session_context` create body is older than the web's v1alpha2 `config` body. |
| ducktape API notes | A GET-only write-up with a cookie or bearer. It was the source for the watch feed's `web_claude_ai` platform requirement and its token lifetime. |
| `lipefur/claude-rc-api`, Exhen `claude-code` 2.1.88 source | Older snapshots of the same ground. Read for drift only (sessions.md §11). |
| cc-tmux-api, rclaude, otherside-cli, ahp | Evaluated in the scratchpad. Nothing was taken from them. |

## Auth

`credentials.resolve_auth` picks the first credential it finds, in this order:

1. Explicit arguments: `credentials`, `token`, `session_key`, `cswap_slot`. The CLI flags are
   `--token`, `--session-key` and `--cswap-slot`.
2. The environment: `CLAUDE_AI_ACCESS_TOKEN`, then `CLAUDE_RC_ACCESS_TOKEN`, then
   `CLAUDE_CODE_OAUTH_TOKEN`. These are bare bearer tokens and are not refreshed.
3. `CLAUDE_AI_SESSION_KEY`, the claude.ai `sessionKey` cookie. The base URL moves to
   `https://claude.ai`.
4. `CLAUDE_RC_CSWAP_SLOT`, a claude-swap slot read through `scripts/account_inject.py`'s
   `slot_token`. The client never writes it back. When the token expires, it asks cswap again
   and does not refresh it itself.
5. `~/.claude/.credentials.json`. It is refreshed and written back when it expires.

`CLAUDE_AI_ACCESS_TOKEN` is the access token of a full Claude Code login, taken from the macOS
Keychain entry or `~/.claude/.credentials.json`. Sessions routes need the
`user:sessions:claude_code` scope, which the full login has. **A `claude setup-token` cannot reach
sessions.** It is granted `user:inference` only (surface.md §1.2). Expect 401 or 403; nobody has
tried.

The org UUID is resolved in this order: `--org`, then `CLAUDE_AI_ORG_UUID` or
`CLAUDE_RC_ORG_UUID`, then `~/.claude.json`, then the credentials file. If none is set, the client
calls `GET /api/oauth/profile` once (this needs `user:profile`). Tokens are never logged or printed.
`whoami` and the smoke script print only where the credential came from.

## API map

**Headers.** Every `/v1/code/*` call sends what the web sends (HAR):

- `anthropic-version: 2023-06-01`
- `anthropic-beta: ccr-byoc-2025-07-29`, plus any per-call betas
- `anthropic-client-platform: web_claude_ai`
- `anthropic-client-feature: ccr`
- `x-organization-uuid`
- `Authorization: Bearer` (or the cookie)
- `X-Trusted-Device-Token`, when one exists

Scheduled tasks add `ccr-triggers-2026-01-30`. The Files API uses
`files-api-2025-04-14,oauth-2025-04-20`. `/api/oauth/profile` gets the bare OAuth set. All of it
passes through one method, `RemoteControlClient._send`. That method handles one renewal after a
401, retries 429 (honouring `retry-after`), and retries 5xx only for idempotent methods.

**The web platform matters.** A Remote Control worker treats a user message as human only if it
came from a web, iOS, Android or desktop platform (sessions.md §8).

**Routes:**

| What | Route | Evidence |
| --- | --- | --- |
| create | `POST /v1/code/sessions` (v1alpha2). `legacy=True` sends `POST /v1/sessions` instead | HAR #16, §3 |
| list / get / update title | `GET /v1/code/sessions[?statuses,tags,exclude_tags,trigger_id,cursor]`, `GET`/`PUT /v1/code/sessions/{id}` | HAR #21/#22, §4 |
| tag | `PUT /v1/code/sessions/{id}` `{add_tags, remove_tags}` | §4.3 |
| archive / unarchive | `POST …/archive` (409 means already archived), `POST …/unarchive` `{}` | §4.4 |
| delete | `DELETE /v1/code/sessions/{id}`, falling back to `DELETE /v1/sessions/{id}` | **guess** |
| mark read | `POST …/mark_read {event_id}` | HAR #11 |
| share | `GET …/share`; the write is a guess | HAR #24 |
| liveness | `POST /v1/code/sessions/heartbeat_check {session_ids: [cse_…]}` | HAR #13 |
| send | `POST …/events {events:[{payload}]}` | HAR #18 |
| history | `GET …/events?limit&sort_order&cursor` | HAR #26/#34 |
| live stream | `GET …/events/stream?from_sequence_num=N[&control_only=1]` (SSE) | HAR #36, §7 |
| account feed | `GET /v1/code/sessions/watch` with a resume token from `GET /v1/code/sessions?limit=1` | §4.11, ducktape |
| transcript rows | `GET …/teleport-events` | §4.10 |
| environments | `GET /v1/environment_providers`, the private org route, `POST …/cloud/create`, `GET /v1/code/last_used_environment` | HAR #2/#28, surface §2 |
| scheduled tasks | `GET`/`POST /v1/code/triggers`, `POST /v1/code/triggers/{id}`, `POST …/{id}/run`. There is no delete; use claude.ai/code/routines | surface §3 |
| branch status | `POST /v1/code/github/get-batch-branch-status` | HAR #1 |
| title + branch | `POST /api/organizations/{org}/dust/generate_title_and_branch` | HAR #17, CLI |
| attachments | `POST /api/oauth/file_upload` (multipart `file`) → `file_uuid` | surface §4.4 |
| TTS | `wss://claude.ai/api/ws/text_to_speech/text_stream` | HAR #56 |

Pinning a session sent **no request**. It is client-side only (the `pinnedOrder` in local
storage), so there is nothing to implement.

**Create body (web, HAR #16):**

```json
{"title": "…", "environment_id": "env_…",
 "config": {"sources": [{"type": "git_repository", "url": "https://github.com/O/R"}],
            "outcomes": [{"type": "git_repository",
                          "git_info": {"type": "github", "repo": "O/R", "branches": ["claude/<slug>"]}}],
            "model": "claude-opus-5-5", "effort_level": "medium", "append_system_prompt": "…"},
 "events": [], "tags": ["cowork-warm-start"]}
```

- A `ccpool_` id goes in `self_hosted_runner_pool_id` instead of `environment_id`.
- The web creates the session with no events. Its first `/events` POST carries
  `[set_permission_mode control_request, user message]`; this is `defer_events=True`.
- The CLI puts its initial events in the create body, in the order set_max_thinking_tokens,
  then set_permission_mode, then the prompt (§3.4). That is the default here, so creating a
  session is one request.
- The web signs every event with `device_attestation`. This client cannot sign, so it sends the
  events unsigned.

**Event vocabulary.**

- Controller payloads:
  - `user`: string `content`, `session_id`, `parent_tool_use_id: null`, and optionally
    `priority` and `file_attachments`.
  - `control_request`, with subtypes such as `interrupt`, `set_model`, `set_permission_mode`,
    `set_max_thinking_tokens`, `apply_flag_settings`, `initialize`, `mcp_*`, `rewind_*`,
    `get_*` and `end_session`. `interaction.py` mirrors the cloud worker's allow and reject
    lists.
  - `control_response`
  - `control_cancel_request`
- Worker prompts: `control_request` with `can_use_tool`, `request_user_dialog`, `elicitation` or
  `hook_callback`. A prompt counts as pending until a `control_response` or
  `control_cancel_request` arrives with the same `request_id`
  (`interaction.find_pending`).
- Stream meta frames are yielded as typed events by `stream_session`: `session_update`,
  `security_update`, `delivery_update` and `catch_up_truncated`.

**Permission answers.** Each answer is a `control_response` whose response is a `PermissionResult`:

| Action | `behavior` | Other fields | `decisionClassification` |
| --- | --- | --- | --- |
| Allow once | `allow` | `updatedInput`, `tool_name`, `toolUseID` | `user_temporary` |
| Always allow | `allow` | the above plus `updatedPermissions`, from the request's `permission_suggestions` | `user_permanent` |
| Deny | `deny` | `message`, optional `interrupt` | `user_reject` |

**AskUserQuestion encoding.** The answer is an allow whose `updatedInput` is the original input
plus `answers`, keyed by question text:

- A single-select question maps to its label, or to the "Other" text.
- A multi-select question maps to a list of labels, with at most `len(options) + 1` entries; the
  extra one is "Other". The worker joins the list with `", "`.
- Skip sends empty `answers` and `annotations[q].unanswered = "skipped"`.
- `annotations[q].notes` carries notes.

The worker refuses answers in three cases: the input already had answers, a list was given for
a single-select question, or a key is not one of the question texts. This comes from the
worker's admission code (`a_0257144446.js`), not from a captured web answer.

**TTS (HAR #56).**

1. The websocket opens `…/text_stream?output_format=opus_48000_32&voice=mellow&tts_speed=1.20&client_platform=web_claude_ai&organization_uuid=<org>`.
2. The client sends one or more `{"type":"text_chunk","text":…}` frames, then
   `{"type":"close_stream"}`.
3. The server sends binary frames, each a raw Opus packet (48 kHz mono, 20 ms, TOC 0x68 or
   0x78), then `{"type":"SpeechComplete","audio_sent_ms":N}`.

`tts.ogg_opus` muxes the packets into Ogg Opus. The 436 captured packets decode to exactly
8720 ms, which `ffprobe` confirmed.

## CLI and smoke test

`claude-rc` keeps the 0.3.0 commands and adds these:

- Sessions: `create`, `unarchive`, `delete`, `tag`, `share`, `mark-read`
- Environments and scheduled tasks: `envs list|get|create`,
  `triggers list|get|create|update|run|enable|disable|runs`
- Prompts: `pending`, `answer allow|always|deny|question`. Questions take
  `--select`, `--multi`, `--other` and `--skip`, with an `N:` prefix to target question N.
- Steering: `commands list|run`, `control <subtype> --field k=v`, `model`, `effort`, `mode`,
  `interrupt`
- Feeds and media: `watch` without a session id (the account feed), `speak` (writes `.ogg`, or
  to stdout), `upload`

The read commands take `--json`. `claude-rc web` can now also create a session and answer
AskUserQuestion cards.

`scripts/claude-rc-smoke` is the live check, to be run by a human once a credential exists.
It runs these steps in order:

1. List environments.
2. Create a `claude-haiku-5-5` session.
3. Send "reply with exactly PONG" and check for PONG.
4. Run an AskUserQuestion turn, answer "Blue", and check the reply.
5. Rename the session.
6. Set the model.
7. Archive the session.
8. Delete it, unless `--keep` is passed.

Each step prints PASS or FAIL with the HTTP status of its last request. It has not been run.

## VERIFIED OFFLINE

Run with `HOME=$(mktemp -d) PYTHONPATH=python uv run --no-project --with httpx --with pytest --with websockets pytest -q tests/claude_rc*_test.py`.

- Headers: the web set and the bare OAuth set, the trusted-device token, cookie and bearer modes,
  per-call betas.
- The single `_send` path: 401 renewal, 429 and `retry-after`, 5xx retried only for idempotent
  methods, multipart resend.
- The auth order. cswap slot reading, using AST extraction of `slot_token`.
- The org lookup from `/api/oauth/profile`.
- The create body against HAR #16 (sources, outcomes, model, effort, append_system_prompt, tags).
  The deferred first send against HAR #18. Initial event order per §3.4.
- `mark_read {event_id}`, `heartbeat_check` with cse_ ids, the share GET, events paging, the
  batch branch status body.
- Environment paging and the default pick (the CLI's teleport rule). Trigger bodies and the
  update field whitelist.
- PermissionResult and AskUserQuestion encodings, checked against the 2.1.312 schema and worker
  code. Pending-prompt detection.
- The SSE meta frames, and the watch's token refresh, resync and 410 handling.
- The TTS frame exchange against a fake server. The Ogg muxer on the real captured packets.
- The CLI subcommands and the web UI's create and answer routes.

## NEEDS LIVE CHECK

Collected from every implementer's report. Each is also flagged "unverified" in a docstring.

**Auth and headers**

- Whether a Bearer OAuth token on api.anthropic.com is accepted with the web header set
  (`ccr-byoc` beta plus `anthropic-client-feature: ccr`) the way the cookie web app is. The CLI
  sends no beta on `/v1/code/*`. This also applies to `list_environments`.
- Whether claude.ai `/api/organizations/...` routes accept a Bearer token: `generate_title_and_branch`,
  `get_code_repo`, the repo list (HAR #15) and the branch list (HAR #3). The last two were not
  implemented for this reason.
- The cookie-mode org lookup: `GET https://claude.ai/api/organizations` is assumed to return a
  list whose first item has `uuid`.
- Whether a setup-token (`user:inference` only) gets 401 or 403 on sessions, environments and
  triggers.
- Unsigned events. The web signs each one with `device_attestation`. An org that requires
  trusted or bound devices may refuse unsigned events.
- The cswap path has only run with a fake `slot_token`. claude_swap is not installed here.
- A 429 retry for a POST assumes the request was not stored. Ingest dedupes by event uuid,
  which stays stable across retries.

**Sessions**

- The `delete_session` route: `DELETE /v1/code/sessions/{id}`, with a fallback to
  `DELETE /v1/sessions/{id}` on 404 or 405. Both are guesses.
- The `set_share` write: both the method (PUT) and the body (`visibility_type` / `sharing_settings`)
  are guesses.
- Create with inline events, the CLI form, from a bearer client with web headers.
  `defer_events=True` is the form the HAR proves.
- The `legacy=True` `POST /v1/sessions` body, including the format of its `sources`.
- `query_sessions` with repeated `tags` parameters and list-valued `exclude_tags`.
- The `unarchive_session` response shape.
- `move_to_cloud`: its body comes from the CLI and has never been exercised. Only bridge
  sessions are eligible.
- `mark_read` with the legacy 0.3.0 `{sequence_num}` body. The `{event_id}` form is confirmed.

**Environments and scheduled tasks**

- The envelope of the private org environment list (`?limit=1000`).
- The 200 shape of `GET /v1/code/last_used_environment`. Only a 404 has been seen.
- `allow_mcp_servers` in the cloud/create body.
- `get_self_hosted_pool` with no org header and no beta, and with a normal user token.
- The `GET /v1/code/triggers` envelope and paging. The client assumes `{data}` plus
  `next_cursor`.
- The `…/run` response, and the `POST /v1/code/webhook-triggers` body.
- `fire_routine`: its route, the `experimental-cc-routine-2026-04-01` beta, the `{text}` body and
  the reply. None of these appear in any spec. It is also the one direct httpx call, because it
  uses a per-routine token.

**Interaction**

- The `send_task_message` field name `message`.
- The reply shapes of `list_models`, `get_plan`, `get_session_cost`, `get_settings`, `mcp_call`,
  `list_directory` and `read_file`.
- The values for `set_view_mode` and for `message_rated` sentiment.
- Whether the web's Skip sends empty `answers` plus `unanswered: "skipped"`.
- Whether the web sends a multi-select answer as a list or as the pre-joined string.
- That `request_user_dialog` answers use `behavior`; the old `cli_dialog_response` used `status`.
- That `PermissionResult.toolUseID` sits inside the decision, and that allow answers carry
  `tool_name`.
- Which `WEB_CLIENT_COMMANDS` the browser handles and which go to the worker as `/name`.
- The watch timing: a token is fresh for 225 s and dead after 240 s, syncs come every 100 to
  120 s, `keepalive_tolerance_seconds=135`, and the read timeout is our own choice.
- Whether the watch route works for a non-web platform.
- `control_only=1` for a web controller.
- `mcp_status` with `liveReply`. It replies only on the ephemeral channel, so polling history
  times out.

**Files and TTS**

- Whether the TTS websocket accepts `Authorization: Bearer`. Only the cookie was captured.
- The shapes of TTS error and status frames. Errors are detected by heuristic.
- Voices other than `mellow`, and speeds other than 1.20.
- The Ogg `pre_skip` of 312. The duration comes from the raw granule.
- The host for `/api/oauth/file_upload` with a bearer token: api.anthropic.com is assumed
  (sessions.md §10). Also whether the added `anthropic-version` and User-Agent headers are
  accepted there.
- The `file_kind` values. The client assumes `image`.

## Left as is

`client.stream_events`, the 0.3.0 path, still drops `session_update` and `delivery_update` frames.
`stream_session` is the path that keeps them.
