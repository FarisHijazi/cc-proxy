# cc-proxy

`scripts/cc-proxy-acp` is the ACP client adapter for T3's existing local-command
provider. HQ remains the only session/action owner; MCP supplies compact discovery,
and selected-session detail/actions use the private HQ origin. Never spawn/resume a
replacement Claude process, map remote workdirs onto the local T3 workspace, retry
an uncertain write, or guess native/tmux equivalence. T3's explicit external-session capability gates passive attachment and native controls.
T3's `autoImportProjectId` opts an observable provider into the existing server's
discovery loop (30-second delay between passes). Discovery creates only sidebar records; the history
owner loads on selection. Preserve archived/deleted views and native workers.
The necessary patches against a pinned official nightly are maintained in `patches/t3/`
and installed through the existing service updater; never create a second T3 deployment.
Read README "Existing fleet sessions in T3 Code", `patches/t3/README.md`, and
`docs/testing/fleet-e2e-matrix.md` on demand.

Fleet is a discovery connection. `t3.codes/externalSessionIdentity` v1 carries
the owner-supplied Claude Code/Codex engine, connection, optional observed model
and effective capabilities. Keep the existing opaque ACP bindings; never swap
an imported thread to a native driver that launches another worker. Metadata-only
events must survive serialization and preserve activity timestamps. New drafts
exclude discovery connections; existing lazy bindings retain them while loading.
The management/list handshake must advertise the same versioned capability as
selected observation; otherwise the bridge intentionally omits catalog identity.
Session connections uses ProviderSettingsPanel's existing environment selection;
never gate it on a primary/local backend or add a second selection owner.

Only exact Codex owner rows with the complete ended/read-only contract may
load archived native history. Preserve native UUID and route binding, cancel
pending controls on that transition, and keep all actions disabled. Never
resume a thread for a read or treat an ordinary offline row as empty history.

The eight-patch T3 series includes self-hosted Authentik sign-in through the existing
EnvironmentAuth/SessionStore, AuthSessionRepository and ServerSecretStore owners.
Keep `T3CODE_OIDC_REQUIRED=true` with the private config-file path in the existing
`service.env`; do not trust forwarded identity headers or make legacy pairing a
remote fallback. Browser refresh secrets remain HttpOnly; native credentials use
the existing secure stores. Sign-in/renewal wakes the existing primary connection
supervisor after an auth rejection; preserve that recovery without another socket
owner. Lost refresh responses reuse a grant/session-bound 60-second receipt in the
existing private ServerSecretStore; that server store is not encrypted. Desktop
credentials use Electron safeStorage encryption. Root shell selection follows
committed route matches while an OIDC callback is pending, preserving Settings'
sidebar context. Safari/Home Screen needs only the served web build; stock native clients
need matching app changes for renewable OIDC sign-in.
Read `docs/self-hosted-t3-auth.md` on demand. Maintain the patch series when updating
T3; an official binary does not implement these OIDC flags. Do not add a second
service, relay, credential writer, or automatic update policy.

`cc-proxy-ui --mcp-upstream` exposes the existing authenticated HQ MCP on the same
dashboard listener. `scripts/account_mcp_relay.py` owns transport forwarding only;
HQ remains the fleet, action and OAuth token verifier. Preserve dashboard SSO and
never add credentials or retry writes in the relay. Read README "Fleet MCP for
Claude.ai" and `docs/devlog/claude_20261115-fleet-mcp.md` on demand (plain paths).

Local Rust proxy that speaks the Anthropic Messages API and routes each request
to a backend based on the model name. Repo: `FarisHijazi/cc-proxy`. It started as a
GitHub fork of [fcakyon/claude-code-with-codex](https://github.com/fcakyon/claude-code-with-codex)
and was republished on 2026-10-27 as a standalone repo.
Local checkouts keep the directory name `claude-code-with-codex`, because launchers reference
that path. The ftower deployment runs from a git checkout at `~/src/cc-proxy`, detached at the
deployed `main` commit, which the `claude-account-proxy` unit's ExecStart points at. Upstream conventions live in
[AGENTS.md](AGENTS.md) and still apply.

Interactive session programmability reuses HQ's existing native command owner.
`python/cc_proxy/session_client.py` owns the shared HTTP client; ACP delegates to
it. Closing a client only detaches, and uncertain commands must never be retried.
`scripts/session-mod` publishes passive typed observations into the same HQ
registry, using exact conversation UUIDs and deduplicated event batches. The
launcher's saved `session_mod: true` uses the existing forward proxy; HQ's key
stays on the proxy owner. Direct HQ configuration remains optional. The opt-in applies only to new interactive sessions;
do not reload or stop existing sessions to enable it. SDK/API and configuration
contracts: `docs/session-sdk.md` and `scripts/session-mod/README.md` (plain links).

## The constraint that drives everything

**No paid API keys.** Every backend runs on a login that already exists: the
Claude Code subscription, the Codex CLI login, `cursor-agent login`, a
signed-in Google account. Before adding a backend, check it can work without a
per-token-billed key — if it cannot, it does not belong here. The owner explicitly authorized
Moonshot Open Platform API-key use for Kimi on 2026-11-19, and a Z.ai API key for GLM (`zai`) on 2026-11-24;
these are the only vendor-key exceptions.
On 2026-11-23 the owner also authorized one Anthropic Console API key as a pool member tried before
the subscriptions; the proxy's own monthly budget stops it before Anthropic does (quota fallback paragraph).

## Architecture

One `Provider` trait (`src/provider.rs`) per backend; `src/registry.rs` maps a
model name to one. `src/server.rs` owns the routes. Translation helpers shared
by every backend live in `src/providers/translate_shared.rs` — prefer extending
that over duplicating per-backend logic.

| Backend | Talks to | Login |
| --- | --- | --- |
| `anthropic` | api.anthropic.com, byte-passthrough | Claude Code's own |
| `codex` | OpenAI Responses API | Codex CLI `~/.codex/auth.json` |
| `kimi`, `grok`, `cursor` | each vendor's API | own, per backend |
| `gemini` | gemini-web-api, OpenAI-compatible | none — lives in that server |
| `cursor-cli` | the `cursor-agent` binary | none — reuses the CLI's |
| `agy` | the Antigravity `agy` binary | none — reuses the CLI's |
| `zai` | Z.ai's Anthropic-compatible Messages API | API key file `zai/api_key` |

## What this fork adds

Design rationale, including the alternatives rejected and why, is in
`docs/devlog/claude_20260915-gemini-cursorcli-auth.md`. Read it before changing
any of the three.

1. **`gemini`** (`src/providers/gemini/`) — Anthropic ⇄ OpenAI chat-completions
   against a configurable base URL, so it also serves any other
   OpenAI-compatible server. Chat only, by design.
2. **`cursor-cli`** (`src/providers/cursor_cli/`) — spawns headless
   `cursor-agent` and maps its `stream-json` events to Anthropic SSE.
3. **Optional inbound auth** (`src/inbound_auth.rs`) — off unless a token is
   configured.
4. **`agy`** (`src/providers/agy/`) — spawns headless Antigravity `agy` and maps its
   `stream-json` steps to Anthropic SSE, sharing `cursor_cli`'s run loop
   (`AgentTranslator`), prompt rendering and workspace detection. No existing bridge
   reuses agy's own login: the HTTP ones (CLIProxyAPI, antigravity-claude-proxy) call
   Cloud Code with their own OAuth, agy-headless-bridge is MCP only.

## Traps

1. **`cursor-agent` emits the full reply twice**, and `timestamp_ms` does not
   tell you which is which — a multi-step run was observed sending the settled
   message *with* a timestamp. `events.rs` compares against `segment` (the
   message being built) instead: equal means settled, extending means a tail,
   anything else is a delta, and `segment` resets at each thinking/tool_call
   boundary. Do not "simplify" this to a timestamp check or a substring
   search; both were tried and both corrupt output. Four regression tests
   guard it.
2. **`--trust` belongs on every cursor-agent mode.** It answers the
   directory-trust prompt, which headless runs cannot answer, so without it
   even `ask` mode refuses to start. The write grant is `--force`, and that is
   what must never appear in a read-only mode.
3. **`cursor-cli` never returns `tool_use`.** The agent already ran its tools.
   Advertising Claude Code's tools or emitting `tool_use` would make Claude Code
   re-run work that is already done.
4. **Write access is gated twice** — the `cursor-cli-agent:` prefix *and*
   `allowWrite`. Read-only modes must never pass `--force`/`--trust`; there is a
   test asserting exactly that.
5. **Inbound auth must not use `Authorization` alone.** That header carries
   Claude Code's subscription token to the Anthropic passthrough. The proxy
   credential goes in `x-claude-codex-key`; `Authorization` is only *also*
   accepted, and a non-matching one is not an error.
6. **Kimi's translator is not shared.** It is coupled to kimi specifics; the
   gemini backend has its own. `kimi::count_tokens` *is* generic and is reused
   by both new backends, as is `anthropic::accumulate_response` (SSE -> one
   Messages JSON), which every streaming backend needs for its non-streaming
   path.
   `providers/kimi/translate/model_allowlist.rs` owns endpoint-aware Kimi model identities. Registry consumes
   `public_model_ids()` for listing and `all_public_model_ids()` for routing. Moonshot Open Platform uses
   `CCP_KIMI_BASE_URL=https://api.moonshot.ai/v1` and prefixed upstream IDs. A kimi.ai Coding account uses
   `CCP_KIMI_OAUTH_HOST=https://auth.kimi.ai` and `CCP_KIMI_BASE_URL=https://api.kimi.ai/coding/v1`.
   Kimi has two credentials, tried in order by `providers/kimi/client.rs` `lanes()`: the Kimi login
   (`kimi/auth.json`, OAuth, at `CCP_KIMI_BASE_URL`), then the Moonshot Open Platform API key
   (`kimi/api_key`, plain text, mode 600, at `CCP_KIMI_API_KEY_BASE_URL`, default `https://api.moonshot.ai/v1`).
   A 401/402/403/429 on one hands over to the next; a spent quota or balance is never retried; the last
   lane's 429 reaches the fallback chain. Never write an API key into `auth.json`: it replaces the login.
   Kimi streams incrementally. Unsupported model IDs and content fail explicitly.
   `tests/kimi_parity.rs` covers streaming, cancellation, errors, and fallback before output.
7. **`cursor-cli` follows the caller, and `--workspace` is not a sandbox.**
   An Anthropic request carries no working directory, so
   `cursor_cli::workspace` reads it out of the environment block Claude Code
   prepends (`- Primary working directory: <path>`, captured off real traffic).
   A configured `cursorCli.workspace` still wins. But a pinned instance was
   measured reading an absolute path handed to it in the prompt, outside the
   pin: `--workspace` sets where the agent *starts*, not what it may touch.
   Containment is the mode (`ask`/`plan` cannot write) and the proxy user's own
   file permissions.
8. **Listings are availability-gated; routing is not.** `Provider::availability()`
   decides whether a backend's models appear in `/v1/models`, the unknown-model
   error and `claude-codex models`; `provider_for_model` ignores it entirely, so
   a hidden id still routes and signing in needs no restart. Tests that assert a
   provider appears must set `CCP_SHOW_ALL_MODELS=1` — two upstream CLI tests and
   one of this fork's own had to be updated for exactly that reason.
9. **Any `gemini-*` id routes to gemini, listed or not.** `GEMINI_MODELS` is
   only what we LIST; `provider_for_model` falls back to gemini for any
   `gemini-` id after the exact-match pass, matching the backend, which already
   forwards unlisted `gemini-*` ids verbatim. So hiding a model is just deleting
   it from the list — the `-thinking` ids are unlisted (gemini-webapi 2.1 serves
   them as plain flash) yet still route for a 2.0.x server. An earlier design
   kept a second "advertised" set on the `Provider` trait for this; the fallback
   made it unnecessary. `registry::tests::everything_listed_can_actually_be_routed`
   pins that every listed id routes.
10. **An optional loopback Messages broker owns the Cursor tool protocol when configured.**
   `CCP_CURSOR_MESSAGES_BASE_URL` makes `src/providers/cursor/messages.rs` relay exact `cursor:<id>` requests
   through the existing Anthropic opaque relay. It rebuilds minimal headers and never falls back to the legacy
   provider. It does not install or manage a broker. The research and evaluation are in
   `docs/research/20261029-cursor-bridge-reuse.md` (plain path). Do not port broker protocol logic here.
11. **The `cursor` backend can borrow `cursor-agent`'s token** from the macOS
   Keychain (`cursor-access-token`/`cursor-user`) when the proxy has no login of
   its own. Read fresh every time, never written to the proxy's store — copying
   it would go stale the moment the CLI refreshed. Not to be confused with the
   `cursor-cli` backend, which spawns the CLI rather than reusing its token.
12. **`gemini` overlaps with `claude-code-router`**, which routes to any
   OpenAI-compatible endpoint from config. It is kept to avoid running a second
   gateway alongside the Claude/Codex subscription logic here — see the devlog
   before extending it. `cursor-cli` has no equivalent anywhere.
13. **agy traps** (all measured on agy 1.5.16, fixtures in `tests/fixtures/agy/`). The turn goes on stdin
   (`--input-format stream-json` + `-p=`): a bare `-p` swallows the next flag as its prompt. A turn past
   roughly 50k tokens loses its END, the newest message, so `agy::keep_newest` caps the prompt. Read-only is
   `--settings` deny rules (`write_file(*)`, `command(*)`), not `--mode plan`: plan mode still tries commands
   and, denied, answers nothing. `text_delta`s are pure deltas and `result.response` repeats them, so it is
   only a fallback. agy's ids include `claude-*`, so only `agy:`/`agy-agent:` route here. Without a keyring
   (SSH, systemd) agy keeps its token in `~/.gemini/antigravity-cli/antigravity-oauth-token`, which
   `scripts/account_agy_quota.py` reads (daily-cloudcode-pa `retrieveUserQuotaSummary`, plus
   `retrieveUserQuota` for the Gemini group a Starter summary omits) and never refreshes.
14. **`zai` routes every `glm-*` id and nothing else** (`src/providers/zai/`). Z.ai speaks the Messages API, so
   the request is re-serialized from `MessagesRequest` (a fallback substitute's model wins) and sent with only
   `x-api-key`, `anthropic-version` and `anthropic-beta`: never the caller's `Authorization`. The key is
   `<config dir>/zai/api_key` (plain text, mode 600; on ftower a symlink to `~/.config/claude-codex/zai/api_key`),
   base URL `CCP_ZAI_BASE_URL` (default `https://api.z.ai/api/anthropic`). `ZAI_MODELS` is only what is listed:
   the ids Z.ai's `/v1/models` lists and that answered on 2026-11-24. GLM's own thinking signatures are rewritten
   to `ccp:zai:v1:<sig>` (`ProxyReasoning::Zai`) on the way out and unwrapped on the way back, so the Anthropic
   passthrough drops them instead of 400ing. Z.ai reports a bad key as 401 and balance/plan trouble as 429 with
   codes 1113/1309/1311: all become a generic 429 (`retry-after: 300`; only the vendor status/code is logged), so the
   default chain `zai, accounts, codex` (GLM ids sit in the sonnet-luna tier) answers and a recovered GLM leads
   again. No default chain ever substitutes GLM for another model.

## Working on it

```sh
cargo build && cargo test
cargo fmt --check && cargo clippy --all-targets
```

A server instance may already be running on `:18765` and be in active use —
check `lsof -nP -iTCP:18765` before starting one, and use a different port
(`PORT=18999 cargo run -- serve`) rather than stopping it.

Unit tests did not catch any of the traps above; all four came out of running
the binary against the real CLI. Exercise both new backends end to end before
calling a change to them done. The gemini path can be driven deterministically
by pointing `CCP_GEMINI_BASE_URL` at any OpenAI-compatible mock.

For keeping a claude.ai login and Remote Control while inference uses this
router, use `scripts/claude-account-router`. Its isolated storage, access-only
credential snapshots, upstream mitmproxy addon and validation are documented in
`docs/devlog/claude_20261018-account-inference-router.md` (deliberately a plain
path, not an `@` include). `src/server.rs` remains the routing owner.

Account endpoints now fall through to the existing Anthropic provider's opaque
relay; inference routes remain model-selected. The endpoint and transport contract,
ToolSearch and message-thread compatibility fixes, and session-owned launcher mode are recorded in
`docs/devlog/claude_20261018-account-endpoint-relay.md` (plain path, read on demand).

The launcher also accepts account aliases and a selectable main login. Main owns
Remote Control, the native Chrome UI, and artifacts. The artifacts MCP adapter receives only
main's login; other accounts remain available for inference fallback. Credential snapshots remain access-only.
Read `docs/devlog/claude_20261018-multi-account-tools.md` for boundaries, upstream
reuse and live validation (deliberately a plain path, not an include).
Local shortcuts and saved account maps are runtime configuration outside this
repository; their setup and smoke checks are recorded in that devlog.

Browsers use only the built-in `claude-in-chrome` tools; there is no custom Chrome MCP (the `account-chrome`
adapter was removed on 2026-11-02, see `docs/devlog/claude_20261102-native-chrome-only.md`). Direct and `--via`
clients use main's native browser roster by default (`+chrome=<main>`, never default `+chromes`).
Legacy explicit `+chromes` clients still use `account_chrome_bridge.py`; new sessions do not merge accounts.
`--chrome-account` explicitly routes the native Chrome bridge to a selected login independently of main. The protocol
boundary and live evidence are in `docs/devlog/claude_20261018-native-chrome-account.md`
(plain path, read on demand).

Native `/usage` follows `--main-account`, just like Claude inference; `+rc=` changes
only Remote Control endpoints. A GPT model does not move Claude subagents to a
different Claude account. Select a main account with remaining quota and a separate
RC owner rather than rewriting usage percentages. Live TUI and regression evidence is in
`docs/devlog/claude_20261024-usage-account-routing.md` (plain path).

Model access differs across Claude accounts: `fallback.on.model_unavailable` defaults to true and
tries the same requested model on the next account after a 404. A model rejection never disables
that account or creates a cooldown; quota/auth errors take precedence over model-not-found errors.
Cursor CLI discovery advertises signed-in CLI models as `cursor-cli:<id>`, one row per model (`listed_models` in
`src/providers/cursor_cli/models.rs`: no `-fast`, highest effort, thinking variant); the legacy `cursor-agent`
id remains a Cursor API alias. See `docs/devlog/claude_20261028-model-access-and-cursor-cli.md` (plain path).

Quota fallback: `account_inject` (the credential owner) attaches every usable login,
session alias first, as `x-claude-codex-account-pool` on `POST /v1/messages` and `/v1/messages/count_tokens`;
`src/fallback.rs` (the routing owner) strips it and walks the tier chain on 429,
plus Claude-account 401 with a bounded credential quarantine. An empty Claude pool
still reaches other vendors. The explicit primary-account marker prevents a filtered
pool from handing thread ownership to a backup; cooldowns belong to each Registry.
Completed fallback routes are keyed only by conversation identity (session plus optional agent),
not by the temporarily usable pool or its primary alias. A remembered account is tried only
if it remains in the current authorized pool and policy chain. A remembered target serving a
different model than requested (Kimi's Claude tier fallback, say) never jumps ahead of the requested
model: it only leads the fallbacks, so a recovered Kimi answers the next turn. Changing the requested model
clears the route; count, title, compaction and auto-review requests do not change it.
Claude model: accounts, then Codex. Codex model: Codex, then Claude accounts.
Cursor is never a default target; it requires an explicitly configured chain. The accounts file's
`fallback` section overrides these defaults (`chains` per family `claude`/`codex`/provider name,
or per model: an exact id or a `prefix*` glob, normalized like tiers, beats the family key; the owner's live
`"claude-fable*"`/`"fable": ["accounts"]` keeps Fable on the same Fable model across accounts and the Console key,
never another model, so an all-accounts 429 reaches the client;
`chains["*"]` = the global order the dashboard's flat Accounts list writes, inherited by every family
without its own chain, with the request's own provider (Claude: its first run of accounts) moved or
added to the front; a provider-name target is that tier's cell on the provider, skipped when the tier
has none. Built-in tier providers retain this meaning; other names that are also routable model ids
(such as `cursor-cli`) are used literally when no tier cell exists;
`accounts:N` = the next N pool accounts, so another backend can sit between
accounts; `accounts` = the rest; `on` = `rate_limit`/`auth`/`server_error`; `threshold` %).
`account_inject` attaches it as `x-claude-codex-fallback` beside the pool (stripped the same way) and alone
applies `threshold`, putting accounts whose last reply reported 5h/7d utilization at or over it last in
the pool (primary marker unchanged). See `docs/devlog/claude_20261027-failover-config-ui.md`. Never relay the pool header upstream, never retry after
output, and never send a thread delta to another account (refuse so Claude Code
resends history). Attempts with a fallback after them run inside
`retry::fail_fast`, including its explicit capture into the spawned HTTP SSE reader,
or Codex's own 429 backoff stalls the chain for minutes. No `cswap switch` is needed;
cswap remains the credential owner rather than the request selector. The accounts
file's `balance` (`session` | `prefer` + `prefer` alias | `round-robin` |
`least-sessions`) picks only a session's first account, keyed by
`x-claude-code-session-id`; never rotate individual requests (per-account caches). Aliases are
emails (pattern shared by the three launcher scripts); the proxy file's `renamed` maps old aliases
that running sessions still send. A client's `main` sets the login claude.ai sees (artifacts,
Remote Control, `/usage`); `balance.prefer` only picks inference. Read
`docs/devlog/claude_20261024-quota-fallback.md` on demand (plain path).
Console API keys: the `--serve` accounts file's `api_key_accounts` (`{alias: {"key", "preferred",
"monthly_budget_usd"}}`, never in the repo or a client) adds pool members of kind `api_key`. A `preferred` key leads
every unpinned pool and owns the thread (`primary`); `balance` still orders the subscriptions after it; other keys
go after them. `account_inject` never puts a key on the request itself, only in the pool; `PoolAccount::sign` sends
it as `x-api-key` without `Authorization`, with `accept-encoding: identity` (Claude Code's OAuth betas and body
fields were measured to work with a key). **Budget:** `scripts/account_key_spend.py` prices each answered reply
(traffic DB `*_tokens` columns, incl. `cache_write_tokens`/`cache_write_1h_tokens`) with `scripts/anthropic_prices.json`,
the only price table (the router `include_str!`s it). `account_inject` re-reads the month's spend every 10 s off the
event loop and leaves the key out while it is unknown, older than a minute or at the budget; otherwise the pool
member carries `budget_usd` left, and `key_budget::estimate_usd` (whole prompt at the 1-hour cache-write price plus
up to 8,192 output tokens) skips any request that might not fit. Spend is only what the proxy sent: other users of
the key are invisible (Anthropic's cost report needs an Admin key). A key's `billing_error`, a 400 saying its credit
is too low or its API usage limit is reached, or an unreadable 400/402, falls back under `on.rate_limit`; the first
two also cool that key for every model until the 1st of next month, 00:00 UTC (in memory only). Incident and
measurements: `docs/devlog/claude_20261123-console-api-key-pool.md` (plain path).

Saved accounts are claude-swap slots: cswap owns every refresh grant, and the launcher
spends an expired non-live slot's grant only through cswap's `consume_backup_grant`.
`AccountInject.renew` (one background thread, at most once a minute per account) renews a login in two cases.
The first is a 401 on a token `account_inject` injected (non-pool requests), which also drops the token from the cache.
The second is a token within `MARGIN_MS` of expiry or already expired, noticed in `token()`. A backup slot is renewed
through cswap's `consume_backup_grant`, and only when it still holds the rejected token. The live login is renewed
by `renew_live_login`, which runs headless `claude -p` with the proxy env stripped, so Claude Code refreshes it under
its own locks. Nothing else on the proxy host renews it. cswap's `_fetch_active_usage` defers an expired live login
whose lineage the stale slot backup cannot vouch for, and Claude Code refreshes only within 5 minutes of expiry, so
renewing 30 minutes early is impossible. Outages: `docs/devlog/claude_20261109-renew-rejected-login.md` and
`docs/devlog/claude_20261110-renew-expiring-live-login.md` (plain paths).
With `CCP_GRANTS_DSN` set (opt-in, for primary + standby proxies), PostgreSQL owns every Claude grant instead
of cswap: a committed lease on generation G before any refresh POST, an encrypted local journal of G+1, and
adoption of the host's live login into the table (`adopt_live_login`, run off the event loop on reloads).
`CCP_AFFINITY_DSN` (separate opt-in) shares session-to-account affinity across proxies; see
`docs/postgres-affinity.md` (plain path). In every mode a login fetch runs on `FETCHER`, never on the event
loop: a still-valid token keeps serving while it refreshes, only a request with no valid token waits
`FETCH_WAIT_S`, and a login still being fetched (`StillFetching`) is skipped for that one pool, never marked
unusable. `CCP_GRANTS_CODEX_ACCOUNT` + `CCP_CODEX_AUTH_FILE` put the
Codex login in the same table (row `codex:<name>`): `account_inject` refreshes it and writes an access-only file,
and `--serve` starts the router with `CCP_CODEX_EXTERNAL_REFRESH=1`, which never refreshes. See
`docs/postgres-grants.md` (plain path).
Live computer-use and real two-account Chrome checks, the `computer-use` flag gate
(appears only from a config dir's second launch) and the fact that 2.1.302 keeps
computer-use approvals terminal-only are in
`docs/devlog/claude_20261018-personal-full-login-live-checks.md` (plain path).

The main client profile shares `~/.claude` through cswap's share-all mirroring and
mirrors `~/.claude.json` preferences (account caches stay per profile); hooks run.
See `docs/devlog/claude_20261019-launcher-shares-user-config.md` (plain path).

`--serve` runs the proxy for other machines with logins injected from that box's cswap
(`scripts/account_inject.py`); `--via` clients hold placeholder logins only, built from the
proxy's profile endpoint. Chrome and artifacts stay on main, while inference pooling happens on the server.
No proxy auth: bind `--serve` to a tailnet IP.
Live on ftower; see `docs/devlog/claude_20261019-shared-account-proxy-serve-via.md` and
`docs/devlog/claude_20261019-tailnet-only-proxy-and-chrome-for-both.md` (plain paths).
`--serve --via` chains proxies: `account_inject.cascades()` leaves aliases this hop
does not hold as placeholders, the router reaches Anthropic through `CCP_ANTHROPIC_PROXY`
(trusting `CCP_ANTHROPIC_PROXY_CA`), and the Chrome bridge gets `server_conn.via`. See
`docs/devlog/claude_20261026-cascading-proxies.md` (plain path).
A client's `"pin_main": true` adds `+pin`, so its sessions start on their own alias, ignoring `balance.prefer`.
`"traffic_db"` in the `--serve` accounts file turns on `scripts/account_traffic.py` (every flow, sender
metadata, no credentials; read with `scripts/claude-traffic`; `"traffic_days"` prunes). See
`docs/devlog/claude_20261027-traffic-database.md` (plain path).
⚠️ **Nothing in a mitmproxy hook may touch the disk, a subprocess or the network**: every hook of every addon
runs on ONE event loop shared by every connection of every account. `account_traffic.py` hooks only copy
plain data into a bounded queue; its writer thread owns SQLite and drops rows when behind. Writing inline
stalled Remote Control for every machine once the DB reached 18 GB
(`docs/devlog/claude_20261112-traffic-writer-off-event-loop.md`, plain path).
Each model-call row also stores `input_tokens` (cache writes included)/`cache_read_tokens`/`output_tokens`/`cache_write_tokens`/`cache_write_1h_tokens` from the reply (parser:
`account_providers.reply_tokens`; NULL = the reply reported none, never zero). `claude-traffic backfill-tokens`
fills older rows. Rows also keep `first_byte` (reply headers) and `first_token` (first streamed `content_block_delta`); open client connections and in-flight model calls stay in memory and the writer thread copies them into the one-row `live` table every 5 s. `scripts/account_metrics.py` serves Prometheus `/metrics` from the same read-only database on the dashboard's private listener (no extra service or dependency). Counters/histograms are process-local: bootstrap the last 15 minutes, then consume new `flows.rowid` values, never sliding-window counters. Restart the UI exporter after replacing/emptying/vacuuming its database; it cannot infer reused rowids. Labels are capped at 256 account/model pairs plus `other/other`, no session/address labels; use `histogram_quantile` for TTFT and output-speed p50/p95. A stale live snapshot omits live gauges and sets `cc_proxy_recorder_up` to 0. `scripts/account_insights.py` derives the dashboard's Insights view (`/api/insights?hours=`, at most 24: a week was measured at 48 s cold) from those, overall and per account and per model: active sessions, connections, machines, TTFT, tokens/s, requests/min, error, fallback and cache-hit rates. No samples or a stale `live` row is unknown, never zero. The dashboard's Models view lists every id the router serves (from `/api/state` providers). The launcher's
`served_models()` adds the same non-Claude ids to the `/model` picker at launch (rows from `served_rows()`).
**The `/model` menu is per host**: `modelPicker.options` in that host's `~/.claude/settings.json`, which the proxy cannot
change; the launcher also snapshots it into `--settings` at launch, so start a new session to see changes. After adding or removing a backend or model, run
`scripts/claude-model-picker` (`--dry-run` first) on every client host: it reads `via`/`via_ca` from the accounts file
(the `--serve` host passes `--via http://<tailnet-ip>:<port> --via-ca <state>/ca/mitmproxy-ca-cert.pem`), replaces its own
rows (description `<provider> via cc-proxy`), keeps every other row and key, writes through the settings symlink with a
`.bak-model-picker-<time>` backup, and exits 1 leaving the file untouched when the proxy lists nothing. The router's Claude ids come from Anthropic's own `/v1/models`. A `/v1/models` call that
carries a login refreshes them at most every 10 minutes (`providers::anthropic::live_models`). `account_inject.py` lends
the first enabled account's login to a token-less one, such as a call from the dashboard or launcher. `ANTHROPIC_STYLE_ALIASES` is only the
fallback list and the alias-routing set. Do not grow it to list models.
The `local` backend is `GeminiProvider::local()`. It uses the same OpenAI chat translation and is configured by `CCP_LOCAL_*` or
`local.*` (README, "Local OpenAI-compatible models"). The proxy host's router binary must be built from this repo,
never from a scratch copy, or the next build silently drops a backend. The dashboard's Usage view (`/api/usage?hours=`) totals calls and tokens per machine, session and
answering account. See `docs/devlog/claude_20261102-usage-by-machine-and-session.md` (plain path).
`serve()` turns SIGTERM into its cleanup, so stopping the launcher stops the router and mitmdump.
Packaging: `pyproject.toml` ships `scripts/` inside the `cc-proxy` wheel; `python/cc_proxy/cli.py` downloads the
router from the release matching the package version, so bump `pyproject.toml` and `Cargo.toml` together.
`Dockerfile` and `compose.yaml` run `--serve`. See `docs/devlog/claude_20261027-cc-proxy-packaging.md` (plain path).
Run tests as CI does with an empty `HOME`: tests that read the machine's Codex login pass locally and fail in CI.
The cold-catalog fallback test runs in an isolated subprocess because its temporary Codex environment must not redirect catalog requests from concurrent integration tests.
The Nix-pinned compiler still needs atomic `fetch_update`; keep its deprecation allowance scoped to `next_monotonic_nonzero` so both Nix and current stable CI compile.
Clients: `scripts/claude-accounts` reads `via` (tried in order by `reach()`, wstunnel `tunnel` entries) and `via_ca`
from the accounts file; profiles are cached a day in `<state>/profiles.json`. Run Python tests with `uv run --no-project`.
Dashboard: `scripts/account_ui.py` + `account_ui.html` (stdlib server, no build). It writes only `order`, `balance`,
`disabled`, `traffic_days`, `fallback`, `session_models`, `api_keys` in the accounts file (privately: it may hold a Console key); usage comes through the proxy with placeholders. `disabled` accounts are
skipped by `AccountInject.pool()`/`start()`. See `docs/devlog/claude_20261027-dashboard.md` (plain path).
The dashboard has hash-routed sidebar views and locally vendored Pico conditional CSS (no build step).
`src/fallback.rs::defaults()` owns the only default chains and equivalence groups, exposed through
`/v1/models` as `fallback_defaults`. The dashboard forwards that metadata without guessing when unavailable.
`fallback.tiers` is an ordered full replacement list of `{name, match, models}`; missing or empty uses
built-ins. Exact matches precede family substring matches. `account_ui.py` remains the only config writer
and validates bounds, duplicate keys, and full upstream `claude-*` destination IDs. Codex aliases remain
valid destinations and async fallback dispatch prepares their catalog before routing. Claude Code
expands native Claude aliases; `[1m]` sets a client budget. Neither is a raw Anthropic destination ID.
See `docs/devlog/claude_20261029-sidebar-equivalent-models.md` for tests and deployment status (plain path).
`GET /api/sessions` lists recent inference sessions from the traffic DB, not proof of running processes.
`session_models` maps session IDs to forced model IDs; dashboard updates patch only named sessions and `null`
clears one. `account_inject` strips client-supplied `x-claude-codex-session-model` and attaches the configured
model for inference/counts; `server.rs` strips it and rewrites only an actual model change before routing.
A changed-model thread delta must negotiate full-history resend. The client's `/model` state is unchanged.
Source-labeled non-Claude usage cards use `scripts/account_providers.py` (24-hour traffic window,
newest 2,000 replies for token totals). `scripts/account_cursor_quota.py` reads Cursor's own quota RPCs,
`scripts/account_codex_quota.py` reads ChatGPT's `wham/usage` (Codex CLI `/status`) and
`scripts/account_agy_quota.py` reads agy's `/usage` limits, each with its
CLI's access token, never refreshing or writing it. Vendor failures are sanitized and isolated;
other providers' unknown remaining quota must not become zero. `scripts/account_kimi_quota.py` reads
Moonshot monetary credit using the existing Kimi access credential and the router's safe endpoint metadata.
Credit is not token quota. `/v1/models` exposes `provider_catalog`; the dashboard consumes its identities.
`src/fallback.rs` owns Kimi equivalence policy in both directions. Provider ordering stays in fallback chains.
`scripts/account_remote_links.py` links each session to HQ (cchq) and its claude.ai mirror (`--hq`/`--hq-public-url`
on `account_ui.py`). Remote Control traffic never carries the Claude session id, and folder/time matching was
measured wrong live; the exact key is the reply `msg_…` id that a worker event relays and the inference response
recorded.
cswap slots may hold `claude setup-token`s (no `expiresAt`, no refresh, inference scope only): `slot_token()` uses them
as-is for both the active login and backup slots; they cannot answer usage/profile, so the dashboard reads their usage
from recorded reply headers. Active/backup parity and the overnight fallback diagnosis are recorded in
`docs/devlog/claude_20261029-overnight-fallback-forensics.md` (plain path).

Self-hosted native Remote Control is an optional launcher route to HQ. `--serve` takes
`--remote-control-backend` too, so every `--via` client's native session reaches the hub; the
routed-session map persists in the proxy state dir. The launcher sets
`ANTHROPIC_CUSTOM_MODEL_OPTION` (default `sol[1m]`) because a proxied session counts as
first-party and Remote Control `set_model` otherwise rejects non-catalog ids. The launcher sets
`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` so bare custom model names also use 1M in new processes.
Codex model bases and derived fast variants come only from
`src/providers/codex/translate/model_allowlist.rs`; the registry consumes that catalog.
Its private `model_catalog.rs` discovers visible API family models on demand, cached for 300 seconds, using
the existing Codex access token without refresh or credential writes. Ownership-compatible legacy CLI cache data and the
compatibility baseline are fallbacks; opaque identity-bearing caches are skipped. `astra`, `sol`, `luna`, and `terra` select the highest numeric version;
explicit IDs stay pinned. Native Claude routing is unchanged. Launcher picker rows and explicit launch model
arguments default to `[1m]`, a client context hint rather than an upstream capacity guarantee. Details and
verification: `docs/devlog/claude_20261029-model-family-aliases.md` (plain path).
The new catalog and live context verification are in
`docs/devlog/claude_20261024-codex-6-models.md` (plain path).
Folder trust
accepted in any account profile carries to every sibling profile. Record: HQ repo
`docs/devlog/claude_20261023-native-ui-and-rc-protocol.md`. The account
proxy owns endpoint selection and credential isolation; HQ owns its native protocol.
Read `docs/devlog/claude_20261018-hq-native-remote.md` on demand (plain path).

The placeholder bearer is `proxy-account-<alias>[+rc=<owner>][+private]`, parsed only by
`account_inject.parse`. `+rc=` puts Remote Control on another login so one claude.ai sees every
account; `+private` (`--private-remote-control` / `TCC_PRIVATE=1`) makes the route addon mark the
HQ registration `x-hq-private: 1` so HQ never mirrors it to claude.ai. It fails closed: the
launcher refuses it without `--via`, and `account_inject` never puts a token on a private
`/v1/code/sessions*` request, so with no route addon Anthropic refuses it. HQ's own mirror sends
`x-hq-mirror: 1`; the route addon strips it and lets the request reach Anthropic (else it loops
into HQ). A caller's `--settings` merges under the launcher's. Read
`docs/devlog/claude_20261023-rc-owner-private-mirror.md` on demand (plain path).
API keys: `account_ui.py` issues `ccp-` keys (`api_keys` holds name, id, prefix and SHA-256, never the key).
`account_inject.api_key()` swaps a matching key for the accounts file's `main` placeholder, so it uses the normal
pool; an unknown `ccp-` key is refused locally. `--serve --api-port` adds mitmproxy's built-in reverse mode for
plain `ANTHROPIC_BASE_URL` clients. See `docs/devlog/claude_20261104-api-keys.md` (plain path).
