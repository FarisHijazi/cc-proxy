# cc-proxy

[![crates.io](https://img.shields.io/crates/v/claude-codex.svg)](https://crates.io/crates/claude-codex)
[![CI](https://github.com/FarisHijazi/cc-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/FarisHijazi/cc-proxy/actions/workflows/ci.yml)

Use Claude Code with your **Claude subscription and your ChatGPT (Codex)
subscription at the same time**, and switch between them mid-conversation.

<img src="https://github.com/fcakyon/claude-code-with-codex/releases/download/v0.3.0/claude-codex-demo.gif" alt="Claude Code running through the proxy" />

It runs as a tiny local proxy. Claude Code already speaks the Anthropic API, so
the proxy sits in front of it and sends each request to the right place based on
the model name:

- Ask for a **Claude** model and it uses your **Claude subscription** (the login
  Claude Code already has). Nothing is translated and no API key is needed.
- Ask for a **Codex model** such as **`gpt-6.1-sol`** and it uses your
  **ChatGPT subscription** through the Codex login.

So you can keep Opus on your Claude plan for hard work and run the fast slot on
your ChatGPT plan, in the same session, and flip between them whenever you want.

[Quickstart](#quickstart) · [Switching models](#switching-models) ·
[How it works](#how-it-works) · [Configuration](#configuration) ·
[Other backends](#other-backends) · [Limitations](#limitations)

## What you need

- **Claude Code** installed and signed in with a **Claude Pro or Max** plan.
- A **ChatGPT Plus, Pro, or Team** plan and the **Codex CLI** signed in.
- **Rust** only if you install from crates.io or source. The prebuilt binary needs nothing.

## Install the account proxy (`cc-proxy`)

One line, nothing to clone. You need [uv](https://docs.astral.sh/uv/):

```sh
uvx --from git+https://github.com/FarisHijazi/cc-proxy cc-proxy --help
```

To keep it as a command on your PATH instead:

```sh
uv tool install git+https://github.com/FarisHijazi/cc-proxy
```

This installs `cc-proxy` (the launcher) and `cc-proxy-traffic` (the traffic reader).
The first `--serve` run downloads the prebuilt `claude-codex` router for your platform
from this repo's release and checks its checksum. A `claude-codex` already on your PATH
is used instead.

### Run the server

Run it on the machine that holds the Claude logins. Add each login with `cswap add`
from [claude-swap](https://github.com/FarisHijazi/claude-swap), then list them in
`~/.config/claude-codex/accounts.json`:

```json
{
  "main": "user@example.com",
  "accounts": {"user@example.com": 1, "other@example.com": 2},
  "traffic_db": "~/.local/state/claude-codex/traffic.sqlite3",
  "traffic_days": 30
}
```

```sh
cc-proxy --serve --listen-host "$(tailscale ip -4)" --port 18790 \
  --accounts-file ~/.config/claude-codex/accounts.json
```

The proxy has no authentication of its own. Bind it only to a private address, such
as a tailnet IP. Clients need its CA certificate, which it writes to
`~/.local/state/claude-codex/account-router/accounts/<main>/ca/mitmproxy-ca-cert.pem`.

### Run the server as a service

Linux (systemd user service):

```sh
uv tool install git+https://github.com/FarisHijazi/cc-proxy
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/cc-proxy.service <<UNIT
[Unit]
Description=cc-proxy account proxy
After=network-online.target

[Service]
ExecStart=%h/.local/bin/cc-proxy --serve --listen-host $(tailscale ip -4) --port 18790 --accounts-file %h/.config/claude-codex/accounts.json
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now cc-proxy
loginctl enable-linger "$USER"     # keep it running after you log out
journalctl --user -u cc-proxy -f   # follow its log
```

macOS (launchd agent):

```sh
uv tool install git+https://github.com/FarisHijazi/cc-proxy
IP=$(tailscale ip -4 2>/dev/null || /Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4)
cat > ~/Library/LaunchAgents/build.cc-proxy.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>build.cc-proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/.local/bin/cc-proxy</string>
    <string>--serve</string>
    <string>--listen-host</string><string>$IP</string>
    <string>--port</string><string>18790</string>
    <string>--accounts-file</string><string>$HOME/.config/claude-codex/accounts.json</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/cc-proxy.log</string>
</dict>
</plist>
PLIST
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/build.cc-proxy.plist
tail -f ~/Library/Logs/cc-proxy.log
# to stop and remove it: launchctl bootout "gui/$(id -u)/build.cc-proxy"
```

Docker (Linux host; mounts this machine's logins, see `compose.yaml`):

```sh
git clone https://github.com/FarisHijazi/cc-proxy && cd cc-proxy
CC_PROXY_BIND="$(tailscale ip -4)" docker compose up -d
docker compose logs -f
```

The CA is in the `cc-proxy-state` volume:
`docker compose exec cc-proxy cat /home/app/.local/state/claude-codex/account-router/accounts/<main>/ca/mitmproxy-ca-cert.pem`

### Connect a client

On every machine that runs Claude Code, with the server's CA saved as `server-ca.pem`:

```sh
uvx --from git+https://github.com/FarisHijazi/cc-proxy cc-proxy \
  --accounts-file ~/.config/claude-codex/accounts.json \
  --via http://SERVER_TAILNET_IP:18790 --via-ca server-ca.pem -- --resume
```

Everything after `--` goes to `claude`. The client's accounts file names the same
aliases. Its `main` decides which login claude.ai sees for artifacts and Remote Control.

## Quickstart

**1. Install `claude-codex`** using a prebuilt binary:

Prebuilt binary, no Rust needed (macOS and Linux):

```sh
curl -fsSL https://raw.githubusercontent.com/fcakyon/claude-code-with-codex/main/scripts/install.sh | bash
```

Or install from crates.io if you have Rust:

```sh
cargo install claude-codex --locked
```

**2. Check your Codex CLI login:**

```sh
claude-codex codex auth status
```

Run `codex login` first if no valid account is found.

**3. Point Claude Code at the router** in `~/.claude/settings.json`:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:18765"
  }
}
```

**4. Start the router** and leave it running:

```sh
claude-codex serve
```

**5. Restart Claude Code.**

Claude Code now discovers the models exposed by the router. Switch directly:

```text
/model gpt-6.1-sol[1m]
/model claude-opus-5
```

The `[1m]` suffix enables Claude Code's larger-context mode. The router removes
the suffix before sending the model name to Codex.

## Switching models

- **Inside Claude Code.** Run `/model gpt-6.1-sol[1m]` for Codex or
  `/model claude-opus-5` for Claude.
- **For one new session.** Set `ANTHROPIC_MODEL` when launching Claude Code.
- **List what is available.** `claude-codex models`.

The Codex catalog includes `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, and
`gpt-6-luna`, plus the existing model IDs. Each has a `-fast` alias for Priority
service tier. Upstream account access still determines whether a request succeeds.

Sessions launched with `scripts/claude-account-router` default custom models to a
1,000,000-token context window, so `/model gpt-6.1-sol` needs no `[1m]` suffix.
This is a client setting, not a separate upstream model. Existing sessions keep
their launch environment; use `[1m]` there or start a new launcher process.
The launcher's default custom picker option is `gpt-6.1-sol[1m]`.

Reasoning is carried across a switch. When you move a conversation from one plan
to the other, the earlier turn's thinking is kept and shown to the next model as
plain tagged text, so context is not lost.

## Usage and account selection

Native `/usage` uses the same account as Claude inference, selected by
`--main-account` or the accounts file's `main`. It does not follow
`--remote-control-account`. To keep phone control on one account while using
another subscription for inference, configure the existing split explicitly:

```json
{
  "main": "inference",
  "remote_control": "controller",
  "accounts": { "inference": 1, "controller": 2 }
}
```

`/usage` shows the inference account's actual limits, not an average across
accounts or another provider's remaining quota.

### Automatic quota fallback (`--serve` / `--via`)

When a request gets a 429 for quota, or a Claude account rejects its credential
with 401, the shared proxy tries the next option before any output reaches Claude
Code. Running sessions keep working without a relaunch or `cswap switch`.
Credential storage and refresh still use `claude-swap`; request selection does not.
An empty Claude account pool does not block Codex or Cursor inference:

| Requested | Tries, in order |
| --- | --- |
| A Claude model | each Claude account in the accounts file, then the Codex model of the same tier, then Cursor |
| A Codex model | Codex, then the Claude model of the same tier on each account, then Cursor |

The accounts file can choose which account a session starts on. The proxy
reloads it on the next request:

```json
"balance": {"strategy": "prefer", "prefer": "personal"}
```

`session` (the default) starts on the session's own account. `prefer` starts every
session on one main account. `round-robin` and `least-sessions` spread new
sessions. Every strategy keeps a session on its account; individual requests
are never rotated, because prompt caches are per account.

To make one machine always start on its own main account, whatever the proxy
prefers, set `"pin_main": true` in that machine's accounts file. Its sessions
still fall back to the other accounts when the main one runs out.

Name accounts by their email, so it is obvious which login is which. Aliases may
use letters, digits, `.`, `@`, `_` and `-`. To rename an alias without breaking
sessions that are already running, map the old name in the proxy's accounts file;
those sessions keep sending it:

```json
"accounts": {"user@example.com": 1},
"renamed": {"personal": "user@example.com"}
```

| Tier | Claude | Codex | Cursor |
| --- | --- | --- | --- |
| Opus | `claude-opus-5-5` | `gpt-6.1-sol` | `cursor:claude-opus-5` |
| Fable | `claude-fable-5-1` | `gpt-6-astra` | `cursor:claude-fable-5` |
| Sonnet, Haiku | `claude-sonnet-5-5` | `gpt-6-luna` | `cursor:claude-sonnet-5` |

The session's own account is tried first, then the rest in accounts-file order.
An exhausted account is skipped until the reset time Anthropic reports. Adding an
alias to the accounts file takes effect on the next request. Cursor uses the
`cursor-agent` login on the proxy host.

### Chaining proxies (`--serve --via`)

A `--serve` proxy can use another `--serve` proxy as its backend. It swaps in the
logins it holds itself, and passes every other alias on, still a placeholder, to
the next proxy, which swaps it there:

```sh
claude-account-router --serve --port 18790 --accounts-file accounts.json \
  --via http://next-proxy:18790 --via-ca next-proxy-ca.pem
```

All of this proxy's Anthropic traffic, including the Chrome bridge, goes through
the next proxy. Each hop runs its own quota fallback over the accounts it holds.
An alias that no hop holds gets a 401 from the last hop.

### Recording all traffic (`--serve`)

Set `"traffic_db": "/path/traffic.sqlite3"` in the `--serve` proxy's accounts file
and it records every request, reply and Chrome-bridge message in SQLite. It takes
effect on the next request, without a restart. Each row of `flows` says when, which
machine, OS user and folder (the launch folder and the session's working directory),
which session and account, the model, the account or backend that served it, and the
status. `--via` clients send their machine, user and folder in headers that the proxy
strips. Clients launched before those headers existed are named by their tailnet IP.

Credential headers and fields are never written. The parts of a conversation that
repeat every turn are stored once, so a busy day is a few hundred MB.
`"traffic_days": 30` keeps 30 days: the recorder deletes older rows itself, at most
once an hour.

```sh
scripts/claude-traffic -n 20          # newest flows
scripts/claude-traffic show FLOW_ID   # one flow, request and reply in full
```

## How it works

Claude Code sends normal Anthropic API requests to the proxy. The proxy reads
the model name and routes:

- **Claude models** are relayed straight to `api.anthropic.com`, untouched,
  reusing the subscription token Claude Code already sends. The request body is
  forwarded as-is so Anthropic's prompt caching keeps working. The proxy stores
  no Claude credentials.
- **Codex models** are translated to the OpenAI Responses API and sent with the
  ChatGPT login from the Codex CLI's `~/.codex/auth.json`. The proxy refreshes
  that token when needed and writes it back so the Codex CLI keeps working.

An unknown model name returns a clear 400 that lists the ids you can use.

## Configuration

Only `ANTHROPIC_BASE_URL` is required in Claude Code's user settings. Restart
Claude Code after changing it so `/model` discovers the router's model list.

| Variable                                   | What it does                                                     |
| ------------------------------------------ | ---------------------------------------------------------------- |
| `ANTHROPIC_BASE_URL`                       | Point Claude Code at the proxy, e.g. `http://localhost:18765`.   |
| `ANTHROPIC_DEFAULT_OPUS_MODEL`             | Optionally remap the Opus alias.                                 |
| `ANTHROPIC_DEFAULT_SONNET_MODEL`           | Optionally remap the Sonnet alias.                               |
| `ANTHROPIC_MODEL`                          | Optionally force one model for the whole session.                |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Leave unset for account features; `1` disables feature flags and Remote Control. |

### Pointing one session somewhere else

`~/.claude/settings.json` wins over the process environment, so
`ANTHROPIC_BASE_URL=... claude` silently does nothing once that file sets it —
the session goes to the configured router anyway. To try a second router
without disturbing sessions already running against the first, override the
settings for that one session:

```sh
claude --settings '{"env":{"ANTHROPIC_BASE_URL":"http://localhost:18766"}}'
```

### Keeping account features while routing inference

A custom `ANTHROPIC_BASE_URL` disables Remote Control in current Claude Code,
even with a claude.ai login. Use the optional
[account router launcher](scripts/claude-account-router) to keep the API origin
at Anthropic while forwarding only inference to this router:

```sh
uv tool install mitmproxy==12.2.3
scripts/claude-account-router -- --model gpt-5.6-sol --remote-control --chrome
```

Keep the existing `claude-codex serve` process running. The launcher starts a
separate loopback forward proxy, gives only its Claude process a private CA,
and snapshots the existing login into isolated storage outside `~/.claude`.
It does not edit `~/.claude`, install a system CA, or change system networking.
`/usage`, account requests and Remote Control stay on Anthropic; only exact
`POST /v1/messages` and `/v1/messages/count_tokens` use the model router.

Account features require a full `/login` credential with `user:profile` scope.
`claude setup-token` credentials cannot enable them. To read another profile,
pass `--login-from /path/to/profile`. If your Python environment has
`claude-swap` installed, `python scripts/claude-account-router --cswap-account 1
-- --model gpt-5.6-sol --remote-control --chrome` reads that saved account
without switching the active account.

Refresh tokens are never copied: relaunch after the access token expires to
read the source's current token. Hooks and plugins from the source profile are
not copied. The launcher supports interactive `--remote-control`; Claude's
`remote-control` server subcommand rejects its required CLI settings.
See [implementation and validation](docs/devlog/claude_20261018-account-inference-router.md)
for routing details, tested features and remaining limits.

### Relaying account endpoints through the router

The router now sends every unclaimed endpoint to `api.anthropic.com`, using the
caller's account credentials. This includes usage, OAuth/account APIs, session
events, files and artifact APIs, plus future account routes. Requests retain
their method, path, query and body; the relay supports streaming uploads,
downloads, SSE, HTTP trailers, and WebSocket upgrades. Redirects, errors,
compression and repeated response headers pass through unchanged. Proxy-only
credentials and hop-by-hop headers are removed. The upstream origin is fixed;
an incoming Host header cannot select another destination.

Model inference/counting and model discovery retain their existing routes.
Disabled local OpenAI-compatible endpoints remain disabled. Optional inbound
proxy authentication also protects account endpoints.

To send all Anthropic API traffic through a fresh router without restarting a
server already in use, run the launcher with `--start-router`:

```sh
python scripts/claude-account-router --start-router --cswap-account 1 -- \
  --model gpt-5.6-sol --remote-control --chrome
```

This requires `claude-codex` and `mitmdump` on PATH, and a Python environment
with `claude-swap` for the optional account selector. It starts a session-owned
model router on an unused loopback port, routes all `api.anthropic.com` traffic
through it, and stops both child proxies when Claude exits. To use an already
upgraded server, pass `--all-endpoints --router http://localhost:18765` instead.
The default inference-only mode remains compatible with older router versions.

Claude Code still refuses Remote Control for a custom `ANTHROPIC_BASE_URL`;
forwarding endpoints cannot change that client check. The launcher retains the
first-party origin with process-scoped proxy/certificate settings. Full account
login and organization permissions still apply; source refresh grants are never
copied. Codex translation preserves ToolSearch references so deferred native
Chrome and MCP tools remain discoverable. When Claude sends a server-side
message-thread delta to a stateless model backend, the router negotiates a
full-history resend; Anthropic-bound threads continue unchanged. See the
[account relay record](docs/devlog/claude_20261018-account-endpoint-relay.md).

### Multiple accounts, one main login

Aggregate native Chrome browsers and read artifacts from several full Claude
logins while choosing one account for Remote Control and default account features:

```sh
python scripts/claude-account-router --start-router \
  --account personal=1 --account work=2 --main-account work -- \
  --model gpt-5.6-sol --remote-control
```

This requires `uv`, plus the launcher dependencies above. Account sources may
also be absolute login directories. To save the selection outside `~/.claude`,
pass `--accounts-file /path/to/accounts.json` containing:

```json
{"main":"work","accounts":{"personal":1,"work":2}}
```

`--main-account personal` overrides that default for a new session. Each main
account gets separate session state. Remote Control belongs to that account;
selecting another browser does not change it. The `account-chrome` MCP browser
list labels each account, and subsequent actions use the selected browser's
native connection. Ask Claude to use `account-chrome` to list or select a browser
across accounts. Native `/chrome` uses main by default. To choose a different
account for the native picker, add `--chrome-account personal` before `--`:

```sh
python scripts/claude-account-router --start-router \
  --account personal=1 --account work=2 \
  --main-account work --chrome-account personal -- \
  --model gpt-5.6-sol --remote-control
```

This routes native Chrome's bridge to the selected login while Remote Control
stays on main. It replaces the aggregate Chrome tools for this session; the
native picker lists only the selected account's browsers. Both accounts need
unexpired full logins. Native CLI permissions and file-upload preprocessing stay
in place; the aggregate adapter still rejects uploads because it cannot run that
private preprocessing. File uploads were not exercised in the live routing test.
The `account-artifacts` MCP tools list across accounts and read metadata or files
with their owning account. They do not publish or share artifacts between accounts.

Expired secondary logins are reported explicitly. Renew the source login and
relaunch; the launcher never refreshes or switches source credentials.
[Implementation and tests](docs/devlog/claude_20261018-multi-account-tools.md).
[Native Chrome routing and validation](docs/devlog/claude_20261018-native-chrome-account.md).

To use a self-hosted HQ Remote Control backend, add these launcher arguments
before `--`:

```sh
--remote-control-backend http://127.0.0.1:8389 \
--remote-control-key-file /path/to/private/native-key
```

The matching HQ backend requires a separate private registration secret. New
native sessions are registered with HQ, while inference keeps using the model
router and other account features keep their existing routes. Anthropic OAuth
credentials are stripped before forwarding to HQ. These sessions are controlled
in HQ; Claude's hardcoded claude.ai session link is not a usable link to them.
The launcher enables `--remote-control` and keeps login storage isolated.
Plain HTTP is allowed only on loopback; use HTTPS or an SSH tunnel remotely.
With `--via`, `--private-remote-control` (or `TCC_PRIVATE=1`) tells HQ to keep the
session to itself and not mirror it to claude.ai. Without `--via` it is an error, and
`--no-private-remote-control` overrides the variable.
See [implementation and validation](docs/devlog/claude_20261018-hq-native-remote.md).

### Putting the router's models in the `/model` picker

Typing an exact id works without any setup, but the interactive picker only
lists models Claude Code knows about. A `modelPicker` lineup adds them, and
`behavesAs` is what stops the unrecognized-model warning below.

```json
{
  "modelPicker": {
    "replaceBuiltInOptions": false,
    "options": [
      { "model": "gemini-3-pro",   "label": "Gemini 3 Pro",     "description": "Google web session, no API key", "behavesAs": "claude-opus-4-8" },
      { "model": "gemini-3-flash", "label": "Gemini 3 Flash",   "description": "Fast Gemini, no API key",        "behavesAs": "claude-sonnet-5" },
      { "model": "cursor-cli-ask", "label": "Cursor CLI (ask)", "description": "cursor-agent, read-only",        "behavesAs": "claude-sonnet-5" },
      { "model": "composer-2.5",   "label": "Cursor Composer",  "description": "Cursor API via cursor-agent",    "behavesAs": "claude-sonnet-5" },
      { "model": "gpt-5.6-sol",    "label": "GPT-5.6 Sol",      "description": "ChatGPT subscription",           "behavesAs": "claude-opus-4-8" }
    ]
  }
}
```

`replaceBuiltInOptions: false` appends these rows and keeps the Claude ones;
`true` replaces the built-in lineup. `behavesAs` takes a **catalog model id**
(`claude-opus-4-8`, `claude-sonnet-5`), not a family alias — it tells Claude Code
which known model's capabilities and context window to assume.

`modelPicker` is documented as user-or-managed scope, so it belongs in
`~/.claude/settings.json`. Measured, `claude --settings '<file>'` also carries
it, which is how to try a lineup without editing a file every other running
session reads.

Claude Code can also discover models from a gateway's `/v1/models`
(`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`), and this router serves that
endpoint — but discovery keeps only ids containing `claude` or `anthropic`, so
it will never surface `gemini-*`, `cursor-*` or `gpt-*`. For these backends the
lineup above is the mechanism, not discovery.

### The unrecognized-model warning

Claude Code only knows the context window of models in its own catalog, so a
router model it has never heard of prints a warning and is assumed to hold
200k tokens — which silently caps auto-compact below what the model can take.
The reply itself is unaffected. Three measured ways to settle it, best first:

```sh
# 1. a modelPicker row with behavesAs — also puts the model in the picker
# 2. the [1m] suffix, for a model that really holds 1M
claude --model 'gemini-3-flash[1m]'
# 3. declare the window outright
claude --settings '{"env":{"CLAUDE_CODE_MAX_CONTEXT_TOKENS":"1000000"}}'
```

The `modelPicker` row silences it completely — both the banner and the
`unrecognized_model` line — measured by running the same prompt with and
without the row.

The `[1m]` suffix is Claude Code's own and never reaches a backend — the router
strips it while resolving the model.

Added by this fork:

| Variable | What it does |
| --- | --- |
| `CCP_AUTH_TOKEN` | Require this token on inbound `/v1/*` requests. Unset = open (the default). |
| `CCP_AUTH_TOKEN_FILE` | Read that token from a file instead of the environment. |
| `CCP_GEMINI_BASE_URL` | gemini-web-api base URL. Default `http://localhost:8100/v1`. |
| `CCP_GEMINI_API_KEY` | Key for that server, if it is deployed behind its own auth. |
| `CCP_CURSOR_CLI_BINARY` | Path to `cursor-agent`. Default: found on `PATH`. |
| `CCP_CURSOR_CLI_ALLOW_WRITE` | `1` lets `cursor-cli-agent:` edit files. Default off. |
| `CCP_CURSOR_CLI_WORKSPACE` | Pin `cursor-agent` to one directory. Default: follow the caller's cwd. |
| `CCP_CURSOR_CLI_TIMEOUT_SECS` | Kill a run after this long. Default `900`. |
| `CCP_CURSOR_CLI_DEFAULT_MODEL` | Model for bare `cursor-cli`. Default `auto`. |

The same settings can live in `config.json` under `auth`, `gemini`, and
`cursorCli`.

Do not set `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY`. Either one overrides
the Claude subscription login and the Claude route returns 401.

The proxy listens on `127.0.0.1:18765` by default. Change it with
`PORT=11435 claude-codex serve`, and match `ANTHROPIC_BASE_URL`.

Alias remapping is optional. For example,
`ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5.6-terra` makes `/model sonnet` use Codex.

## Only what you can actually use is listed

Every backend is probed before its models are offered, so `/model` and the
unknown-model error show what this machine can serve rather than everything the
binary knows how to serve. The probe is cheap and local — a credential file, a
binary on `PATH`, a socket that accepts a connection — and runs only when models
are listed, never on the request path.

| backend | offered when |
| --- | --- |
| `anthropic` | always — Claude Code forwards its own subscription credentials |
| `codex` | the Codex CLI has written `~/.codex/auth.json` |
| `cursor` | signed in through this proxy, **or** `cursor-agent` is signed in |
| `cursor-cli` | the `cursor-agent` binary is on `PATH` |
| `gemini` | something is listening at `gemini.baseUrl` |
| `kimi`, `grok` | signed in through this proxy |

Routing stays permissive: an id that is hidden still routes if you ask for it by
name, so signing into a backend takes effect without restarting the proxy. The
unknown-model error names what is missing and how to get it, rather than letting
a signed-out backend look like one that was never built.

`CCP_SHOW_ALL_MODELS=1` lists everything regardless — useful for seeing what
exists before signing in.

### Cursor without a second login

The `cursor` backend prefers its own `claude-codex cursor login`, and otherwise
borrows the access token `cursor-agent` is already holding (macOS Keychain,
service `cursor-access-token`). Borrowed, never copied: the CLI refreshes that
token on its own schedule, so reading it fresh each time keeps the two in step,
and `cursor-agent logout` takes this backend with it.

That means one `cursor-agent login` lights up **both** Cursor backends — the API
one (`composer-2.5`, `cursor-agent`, …) and the CLI one (`cursor-cli`).

## Subagents on a router model

A subagent runs on whatever its definition names, and Claude Code passes that id
through to the router untouched — so any model the router serves can back one.
Put the id in the frontmatter of a `.claude/agents/*.md` file:

```md
---
name: gemini-reviewer
description: Second-opinion reviewer. Use for reviewing a diff.
model: gemini-3-pro
tools: Read, Grep, Bash
---

Review the diff and report only what is wrong.
```

Claude Code names the subagent in its own telemetry (`agent:custom:gemini-reviewer`
alongside `model: gemini-3-pro`), which is the quickest way to confirm a run went
where you meant. A `gemini-3-pro` subagent drives Claude Code's tools normally —
measured at two tool calls for a read-a-file-and-report task.

The `model:` field takes a full id, not just `sonnet`/`opus`/`haiku`/`inherit`.
The `Agent` tool's own `model` override is a fixed enum and cannot reach these
ids, so an ad-hoc *"spawn a gemini subagent"* with no definition file falls back
to a Claude model — the definition file is what makes it stick.

**`cursor-cli` does not work as a subagent.** It routes — Claude Code reports
`agent:custom:<name>` against `cursor-cli-ask`, and `cursor-agent --mode ask`
processes do spawn — but no run has ever handed a result back. Three attempts,
two agent shapes (one with `tools: Bash`, one with `tools: []`): each looped
through repeated `cursor-agent` invocations for 8+ minutes and had to be stopped.

The cause is structural, not a routing bug. `cursor-cli` never returns a
`tool_use` block — given a request carrying a `Bash` tool it ignored the tool,
ran the command with its own shell, and replied in prose. A subagent on it can
neither use the tool list it was handed nor be held to the parent's permission
mode, so the parent's agent loop has nothing to drive and never terminates.

Use `cursor-cli` as a model you select for a turn and delegate the whole task
to. Not as a subagent.

## Other backends

The same proxy can also route to **Kimi**, **Grok**, and **Cursor** models, each
with its own login. Run `claude-codex models` to see every id, and
`claude-codex <backend> auth status` to check a login. These backends keep
the behavior of the upstream project this is based on.

This fork adds two more, plus an optional lock on the proxy itself. Both new
backends keep the project's premise: **no API key**. Each one runs on a login
you already have — `cursor-agent login` for one, a signed-in Google account for
the other — the same way the Claude and Codex routes run on your subscriptions.

### Gemini (`gemini-web-api`)

Routes to [gemini-web-api], which exposes the `gemini.google.com` web app as an
OpenAI-compatible API using your logged-in Google account. No API key, no
billing. Chat only — image and video generation are out of scope here.

```sh
# 1. start the gemini server (needs Chrome signed in to gemini.google.com)
uvx --from git+https://github.com/FarisHijazi/gemini-web-api gemini-web-api

# 2. check the proxy can see it
claude-codex gemini auth status
```

Then `/model gemini-3-pro` inside Claude Code. Offered ids: `gemini-3-pro` and
`gemini-3-flash`, each also in `-plus` and `-advanced` tiers.

`gemini-3-flash-thinking` (and its tiers) still work if you type one, but are
deliberately **not** listed: gemini-webapi 2.1 removed that tier upstream, so a
current server answers those ids with plain flash. They stay routable for a
server pinned to 2.0.x, which really does have the tier.

There is no separate login: the Google session lives in the gemini-web-api
server, so `auth status` reports whether that server is reachable.

That server sends no usage in its stream, so token counts for gemini turns are
estimated with the same estimator `/v1/messages/count_tokens` uses. A backend
that does report usage keeps its exact numbers.

Because the base URL is configurable, this backend also works against any other
OpenAI-compatible server (Ollama, LM Studio, OpenRouter):

```sh
CCP_GEMINI_BASE_URL=http://localhost:11434/v1 claude-codex serve
```

### Cursor CLI (`cursor-cli`)

Drives the **`cursor-agent` binary you already have signed in**, rather than
Cursor's API. Nothing to log into: run `cursor-agent login` once and the proxy
reuses that session.

```sh
claude-codex cursor-cli auth status
```

That command also lists the models you can use.

The model id carries the execution mode:

| Model id | Mode | Can edit files |
| --- | --- | --- |
| `cursor-cli` | ask, default model | no |
| `cursor-cli:<model>` | ask — Q&A | no |
| `cursor-cli-plan:<model>` | plan — analysis only | no |
| `cursor-cli-agent:<model>` | full agent | **yes**, and it is off by default |

`<model>` is any id `cursor-agent --list-models` prints, e.g.
`/model cursor-cli:composer-2.5`. Only the bare ids are advertised to Claude
Code — Cursor offers a few hundred models and listing every combination would
bury the `/model` picker — but any of them routes.

**It follows the project you have open.** An Anthropic request has no working
directory field, but Claude Code states its cwd in the environment block it
prepends to the conversation, and the proxy reads it back out — so one proxy
serves every project. Set `cursorCli.workspace` to pin every run to one
directory instead.

Note what that pin is: `--workspace` chooses where the agent *starts*, not what
it may touch. An agent given an absolute path in the prompt will read it even
from a pinned instance. What actually contains a run is the mode — `ask` and
`plan` cannot write — and the file permissions of the user the proxy runs as.

The mode holds against a shell redirect, not just the edit tools. An ask-mode
run told to execute `echo SIDE-EFFECT > created_by_shell.txt` answered *"Ask
mode only allows read-only actions"* and left the directory byte-identical. It
does run read-only commands itself, so treat `ask` as "may read anything the
proxy user can read", not as "runs nothing".

**This backend is an agent, not a model.** `cursor-agent` runs its own tool loop
in its own workspace, so Claude Code's tools are not advertised and no
`tool_use` block is returned. One Claude Code turn delegates the whole turn to a
Cursor agent and gets its final answer back; the tools it ran along the way show
up as progress lines.

Write access needs **both** the `cursor-cli-agent:` prefix and an explicit
opt-in, and the id stays out of `/model` until you give it:

```sh
CCP_CURSOR_CLI_ALLOW_WRITE=1 CCP_CURSOR_CLI_WORKSPACE=~/code/project claude-codex serve
```

### Locking the proxy (optional)

The proxy has no inbound auth by default, which is fine on loopback. Set a token
before binding it anywhere else:

```sh
CCP_AUTH_TOKEN=$(openssl rand -hex 32) CCP_BIND_ADDRESS=0.0.0.0 claude-codex serve
```

Claude Code then sends it as a header, alongside its own Claude login:

```json
{
  "env": { "ANTHROPIC_BASE_URL": "http://host:18765" },
  "headers": { "x-claude-codex-key": "<token>" }
}
```

`Authorization: Bearer <token>` is accepted too, for OpenAI SDK clients hitting
`/v1/chat/completions`. It is not *required* there because that header already
carries Claude Code's subscription token on the Claude route — putting the proxy
token in it would break the Claude passthrough. `/healthz` stays open.

[gemini-web-api]: https://github.com/FarisHijazi/gemini-web-api

## Limitations

- Switching plans in the middle of an active tool call (for example pressing Esc
  during a tool use, then switching and continuing) can fail, because the next
  model cannot verify reasoning that came from the other plan. Starting the next
  step fresh avoids it.

## Credits

Started from [`fcakyon/claude-code-with-codex`](https://github.com/fcakyon/claude-code-with-codex),
itself built on [`raine/claude-code-proxy`](https://github.com/raine/claude-code-proxy),
which provides the Codex, Kimi, Grok, and Cursor backends. That project added using
your Claude subscription as a backend alongside Codex, reasoning that survives a
mid-conversation switch, and reading the Codex login from the Codex CLI. cc-proxy
adds the shared multi-account proxy. The installer and demo GIF still come from
`fcakyon/claude-code-with-codex` releases, because cc-proxy publishes none yet.
