# claude-code-with-codex (FarisHijazi fork)

Local Rust proxy that speaks the Anthropic Messages API and routes each request
to a backend based on the model name. Fork of
[fcakyon/claude-code-with-codex](https://github.com/fcakyon/claude-code-with-codex);
`upstream` remote points there. Upstream conventions live in
[AGENTS.md](AGENTS.md) and still apply.

## The constraint that drives everything

**No paid API keys.** Every backend runs on a login that already exists: the
Claude Code subscription, the Codex CLI login, `cursor-agent login`, a
signed-in Google account. Before adding a backend, check it can work without a
per-token-billed key — if it cannot, it does not belong here.

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
10. **The `cursor` backend can borrow `cursor-agent`'s token** from the macOS
   Keychain (`cursor-access-token`/`cursor-user`) when the proxy has no login of
   its own. Read fresh every time, never written to the proxy's store — copying
   it would go stale the moment the CLI refreshed. Not to be confused with the
   `cursor-cli` backend, which spawns the CLI rather than reusing its token.
11. **`gemini` overlaps with `claude-code-router`**, which routes to any
   OpenAI-compatible endpoint from config. It is kept to avoid running a second
   gateway alongside the Claude/Codex subscription logic here — see the devlog
   before extending it. `cursor-cli` has no equivalent anywhere.

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
Remote Control and, by default, the native Chrome UI; separate MCP adapters aggregate browsers
and read artifacts across accounts. Credential snapshots remain access-only.
Read `docs/devlog/claude_20261018-multi-account-tools.md` for boundaries, upstream
reuse and live validation (deliberately a plain path, not an include).
Local shortcuts and saved account maps are runtime configuration outside this
repository; their setup and smoke checks are recorded in that devlog.

`--chrome-account` routes the native Chrome bridge to a selected login independently
of main, replacing the aggregate Chrome adapter for that session. The protocol
boundary and live evidence are in `docs/devlog/claude_20261018-native-chrome-account.md`
(plain path, read on demand).

Native `/usage` follows `--main-account`, just like Claude inference; `+rc=` changes
only Remote Control endpoints. A GPT model does not move Claude subagents to a
different Claude account. Select a main account with remaining quota and a separate
RC owner rather than rewriting usage percentages. Live TUI and regression evidence is in
`docs/devlog/claude_20261024-usage-account-routing.md` (plain path).

Quota fallback: `account_inject` (the credential owner) attaches every usable login,
session alias first, as `x-claude-codex-account-pool` on `POST /v1/messages`;
`src/fallback.rs` (the routing owner) strips it and walks the tier chain on 429,
plus Claude-account 401 with a bounded credential quarantine. An empty Claude pool
still reaches other vendors. The explicit primary-account marker prevents a filtered
pool from handing thread ownership to a backup; cooldowns belong to each Registry.
Claude model: accounts, then Codex, then Cursor. Codex model: Codex, then Claude
accounts, then Cursor. Never relay the pool header upstream, never retry after
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

Saved accounts are claude-swap slots: cswap owns every refresh grant, and the launcher
spends an expired non-live slot's grant only through cswap's `consume_backup_grant`.
Live computer-use and real two-account Chrome checks, the `computer-use` flag gate
(appears only from a config dir's second launch) and the fact that 2.1.302 keeps
computer-use approvals terminal-only are in
`docs/devlog/claude_20261018-personal-full-login-live-checks.md` (plain path).

The main client profile shares `~/.claude` through cswap's share-all mirroring and
mirrors `~/.claude.json` preferences (account caches stay per profile); hooks run.
See `docs/devlog/claude_20261019-launcher-shares-user-config.md` (plain path).

`--serve` runs the proxy for other machines with logins injected from that box's cswap
(`scripts/account_inject.py`); `--via` clients hold placeholder logins only, built from the
proxy's profile endpoint, and aggregate every alias. No proxy auth: bind `--serve` to a tailnet IP.
Live on ftower; see `docs/devlog/claude_20261019-shared-account-proxy-serve-via.md` and
`docs/devlog/claude_20261019-tailnet-only-proxy-and-chrome-for-both.md` (plain paths).
`--serve --via` chains proxies: `account_inject.cascades()` leaves aliases this hop
does not hold as placeholders, the router reaches Anthropic through `CCP_ANTHROPIC_PROXY`
(trusting `CCP_ANTHROPIC_PROXY_CA`), and the Chrome bridge gets `server_conn.via`. See
`docs/devlog/claude_20261026-cascading-proxies.md` (plain path).
A client's `"pin_main": true` adds `+pin`, so its sessions start on their own alias, ignoring `balance.prefer`.

Self-hosted native Remote Control is an optional launcher route to HQ. `--serve` takes
`--remote-control-backend` too, so every `--via` client's native session reaches the hub; the
routed-session map persists in the proxy state dir. The launcher sets
`ANTHROPIC_CUSTOM_MODEL_OPTION` (default `gpt-6.1-sol[1m]`) because a proxied session counts as
first-party and Remote Control `set_model` otherwise rejects non-catalog ids. The launcher sets
`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` so bare custom model names also use 1M in new processes.
Codex model bases and derived fast variants come only from
`src/providers/codex/translate/model_allowlist.rs`; the registry consumes that catalog.
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
