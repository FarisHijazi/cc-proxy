# Account model access and Cursor CLI discovery

## Same model, another account

`src/fallback.rs` previously returned the first account's model-not-found 404,
even when another configured Claude account offered that model. The existing
`fallback.on` policy now includes `model_unavailable`, enabled by default.
The router tries the same requested model on the next account before returning
output. `scripts/account_ui.py` validates the flag and the dashboard edits it.

A 404 does not disable the account or cache its lack of access. If every account
returns 404, repeated requests still return 404. If another account has a quota
or authentication failure, that takes precedence over the earlier 404. An
account cooling down after a quota/auth failure cannot make the next request
incorrectly report that the model does not exist. Thread deltas still require
a full-history resend before switching accounts.

Verified with a real `claude-accounts` client against an isolated candidate:
a client pinned to its own main account requested `claude-mythos-5-1` and received
`MODEL_ROUTE_OK`; the router's fallback log identified a different configured
account serving `claude-mythos-5-1`, unchanged. Bare HTTP probes lacked the
Claude Code request context and were not used as proof of account entitlement.

## Cursor CLI models

The existing CLI provider already executed exact `cursor-cli:<id>` requests,
but model discovery advertised only its three bare selectors.
`src/registry.rs::all_supported_models` now includes the models reported by
`cursor-agent --list-models` under the read-only `cursor-cli:` prefix. The
legacy `cursor-agent` model id still belongs to the separate Cursor API backend.

`src/providers/cursor_cli/models.rs` owns one shared discovery cache: successful
results live for ten minutes and failed lookups for thirty seconds. Cache misses
are single-flight. A five-second timeout kills and reaps the owned listing child.
`src/server.rs` runs the catalog lookup through `spawn_blocking`; unknown-model
errors and CLI summaries keep the short static selector list. The full CLI list
is also available through `claude-codex cursor-cli auth status`.

`src/providers/cursor_cli/process.rs` resolves the default executable through
PATH, then the installer's `~/.local/bin/cursor-agent`. Explicit binary settings
are never replaced. Availability, discovery and inference use the same resolver.
Write-capable selectors remain opt-in and are not advertised.

## Verification before deployment

- Complete Python suite: 162 passed, one skipped.
- Complete Rust release suite with an empty HOME: all targets passed, including
  883 library tests and 21 account-fallback integration tests.
- `cargo fmt --check`, locked release clippy with warnings denied, and locked
  release build passed.
- Cursor discovery tests cover failure TTL, single-flight cache misses, expiry,
  malformed/failed execution and timeout child cleanup. The end-to-end CLI test
  covers installer-path availability, discovery and read-only model execution.
- Independent review found a mixed-404/429 error-precedence bug and blocking
  discovery paths; both were corrected and re-tested before rollout.

## Dashboard account ordering

`scripts/account_ui.html` now stacks account cards in one column. A dedicated
grip handle reorders accounts through the existing `save({order})` path; up/down
buttons remain available for keyboard and touch users. Drag state is separate
from the model-chain editor. A failed save leaves the server-backed order intact.
`tests/account_ui_drag_test.mjs` exercises desktop/phone layout, grip versus
non-grip drags, arrow buttons, account toggles and save failure in an isolated
headless browser with mocked APIs.

## Hostname alias

Both existing Traefik routers on the proxy host now accept a second hostname,
without changing client defaults. Both dashboard routes keep SSO,
and both bearer-gated routes keep reaching wstunnel. Traefik hot-reloaded the
atomic configuration writes without a service restart. The operator's media
repository records the private hostname details and rollback copies.

## Existing-session controls

The Sessions panel uses `GET /api/sessions`, showing recent traffic rather than
claiming that a process is still running. Its default window is seven days and
50 sessions; configured overrides outside that list remain clearable.
`session_models` lives in the existing accounts file. Dashboard writes patch
only named session IDs, with `null` clearing one override. The injector attaches
the configured `x-claude-codex-session-model` only on inference/count requests;
the Rust server removes it before logging or relaying and changes parsed/raw
model fields before selecting the provider. No override means no reserialization.
Changed-model thread deltas negotiate a full-history resend. The client model,
context window and compaction settings do not change. Requests sharing the
session ID, including background calls, receive the same override.

Counts now reuse the existing account-pool dispatch. They retry the same Claude
model across accounts, never substitute another provider, and neither consult
nor update inference cooldowns. Counts with thread deltas never move incomplete
context to another account. Direct non-Claude responses also receive the
router-owned served-by header, so session rows identify actual CLI/Gemini routing.

## Provider usage and quota

The account/provider area includes Codex, Cursor API, cursor-agent CLI, Gemini,
Kimi and Grok. All cards show availability and recorded traffic for 24 hours.
Counts use a single SQL aggregate; token decoding is bounded to the newest
2,000 non-Claude replies and the UI discloses when capped. Historical untagged
rows are classified by model ID, while an authoritative served-by tag wins.
Token-count requests are excluded and malformed bodies lose token figures, not
all dashboard state. Gemini token figures are labeled router estimates.

Cursor's quota helper repeats the CLI `/usage` screen's read-only
GetCurrentPeriodUsage, GetPlanInfo and GetHardLimit calls. It reads the Linux
CLI auth file fresh without writing or refreshing credentials. Included spend
is converted from cents to USD; named-model/API, Auto and the CLI headline remain
separate measures. The API-backed Cursor backend may use a different login,
so it is not assigned the CLI quota. Other providers show unknown remaining
quota, not a made-up percentage. Traffic snapshots cache for 60 seconds and
Cursor quota for 300 seconds, with sources and times displayed.

Credential-bearing calls do not follow redirects. Token whitespace is trimmed;
vendor exceptions, malformed fields and truncated responses are isolated and
shown only as sanitized error categories, never exception strings containing
a bearer. Regression tests cover these cases and failed-snapshot caching.

## Final rollout and verification

- Final Python suite: 206 passed, one skipped; pre-existing deprecation warnings.
- Final Rust release suite with empty HOME: 1,094 tests passed, including 883
  library tests, 24 account fallback tests and seven session-model tests.
- Format, locked release clippy with warnings denied, and release build passed.
- All three isolated-browser suites passed: vertical dragging/arrows, sessions
  force/clear, and provider data/unknown/quota/loading behavior.
- Installed binary SHA-256:
  `584f07e900982a5188f2f71f6d7f7d94b35d5999bd0a5f24e3c1e8740b54faea`.
- Backed up the deployed binary, scripts and accounts configuration before
  swapping files atomically. Restarted only the existing proxy and UI user
  services; client sessions and CA files were not replaced.
- The live acceptance harness passed 36 assertions. It created one isolated
  conversation, requested Haiku, forced Mythos, resumed that same conversation
  still requesting Haiku, and recalled the marker with Mythos served by a
  licensed backup account. It cleared only its own override and verified that
  prior settings/overrides were preserved. A live `cursor-cli:composer-2.5`
  session also returned the marker with CLI served-by attribution.
- The harness initially failed because its isolated HOME hid the installed
  launcher's repository path. Its test-only Projects link was corrected before
  the successful run; production configuration was unaffected.
- A live Mythos token count pinned to a main account without access returned 200
  from an authorized backup, with 12 input tokens and the same requested model.
- Read-only headless checks of the deployed page verified actual provider cards,
  Cursor quota, sessions, account controls and no page overflow at phone width.
  Both public hostnames retained their SSO redirects. A WSS-only client through
  the new hostname returned `NEW_PROXY_HOST_OK`, while legacy routing remains
  accepted. No client hostname defaults were migrated.

## Follow-up: Codex quota and session fixes

- Codex cards now show real plan limits. `scripts/account_codex_quota.py` calls
  `GET chatgpt.com/backend-api/wham/usage`, the endpoint behind the Codex CLI's
  `/status`, with the access token from `$CODEX_HOME/auth.json`. It is read-only,
  follows no redirects and reports only an error class. A live probe on the proxy
  host returned a weekly window. The router's own `codex.rate_limits` events were
  not used because they reach no recorded response header.
- The Cursor reader also reads the CLI's macOS Keychain entry, matching
  `cursor::auth::cursor_agent_token`, so a Mac dashboard host is no longer unknown.
- Forced sessions outside the newest 50 keep their last activity: a second
  query fetches only those overridden IDs.
- The Sessions table has five columns with stacked detail and wrapping controls.
  A browser assertion that Force/Clear fit without sideways scrolling failed on
  the previous layout and passes on the new one at 1280 and 1024 px.

## Follow-up: one fallback list with Codex and Cursor

- The dashboard's account list is now the Claude-model fallback order. Codex and
  Cursor API cards sit in it with the Claude accounts. They can be dragged, moved
  with the arrows, or switched off. Switching a backend off removes it from the
  `claude` chain; the card stays at the end, dimmed. cursor-agent CLI, Gemini,
  Kimi and Grok stay under "Other backends". cursor-agent CLI never returns
  `tool_use`, so it does not fit as a coding fallback.
- `src/fallback.rs` accepts `accounts:N` (the next N pool accounts, in pool order)
  besides `accounts` (every account not yet used). Pool order still puts the
  session's own account first and accounts over the threshold last. The UI writes
  the list as `order` plus `chains.claude`. It drops a custom chain that matches
  the default. Disabled accounts count 0 and keep their place in `order`.
- Tests: `other_backends_can_sit_between_claude_accounts` covers the Rust side.
  `tests/account_ui_drag_test.mjs` covers dragging Codex between accounts, a
  backend toggled off and back on, and arrows, on desktop and phone. Results:
  884 lib tests and the full Rust suite pass, clippy is clean, Python reports
  216 passed and 1 skipped, and all three browser suites pass.
- Deployed: router binary (sha256 `7e1d87f5…`) and `account_ui.html`. Only
  `claude-account-proxy.service` was restarted. Backup is in
  `~/.local/state/claude-codex/before-unified-fallback-20261028/`. A live
  read-only check lists 5 accounts, then Codex #6, then Cursor #7, with no
  horizontal scroll at 1280 or 390 px.

## Follow-up: prefer button, drag anywhere, provider switch, Remote Control links

- Each enabled account card has "Make preferred", which saves
  `balance: {strategy: "prefer", prefer}`.
- Drag uses pointer events. A mouse drags from anywhere on a card except its
  controls. Touch drags only from the handle, so the page still scrolls. A test
  covers each case.
- The Sessions force control is now a provider select, then a model select.
  The session's own model stays selected, even when it is unlisted. Switching
  provider picks the newest same-tier model that is not a fast variant, so Opus
  becomes Sol on Codex.
- Each session shows its cchq and claude.ai Remote Control links.
  `account_remote_links.py` takes HQ's native sessions and matches each one by
  the `msg_…` id of an assistant message in its recorded `worker/events` upload
  against the inference responses from the 180 s before it. A first attempt
  matched on client, folder and time. It was measured on live data and
  mislinked this very session, so it was dropped. Matches are kept for good,
  misses are retried after 5 minutes, and HQ failures show only an error class.
  Live result: 26 of the 50 listed sessions are linked. This session links to
  its own Remote Control session, which was checked against its relayed text.
- The claude.ai links were missing because of a bug in cc-tmux-api's mirror,
  which every session shared one leaking connection pool. That is fixed in
  cc-tmux-api `2d238af`. After the hub restart, all 14 alive sessions showed
  `connected` with a URL.
- Deployed `account_ui.py`, `account_ui.html` and `account_remote_links.py`,
  plus the systemd drop-in `cc-proxy-ui.service.d/hq-links.conf`. Only
  `cc-proxy-ui` was restarted. Backup is in
  `~/.local/state/claude-codex/before-remote-links-20261028/`.
- Results: Python reports 219 passed and 1 skipped. All three browser suites
  pass, including the new touch, prefer, provider and link checks.

Source changes were left uncommitted; no release or push was requested.
