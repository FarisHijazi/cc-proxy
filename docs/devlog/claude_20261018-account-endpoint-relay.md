# Account endpoint relay — 2026-10-18

The user requested complete account-endpoint support in the router while keeping
model routing and normal Claude account features. The earlier
[session launcher record](claude_20261018-account-inference-router.md) remains the
origin-preserving approach for Claude's client-side eligibility checks. This
change extends the server itself and adds an all-endpoint launcher path; its
public contract is in [README.md](../../README.md#relaying-account-endpoints-through-the-router).

## Ownership and implementation

[src/server.rs](../../src/server.rs) still owns route dispatch. Unknown paths
and methods on otherwise model-owned paths go to the registered Anthropic
provider through `Provider::handle_account_request`; they never use the model
alias target. Health checks and disabled local OpenAI surfaces remain local.

[src/providers/anthropic/mod.rs](../../src/providers/anthropic/mod.rs) owns one
transport for both the existing Messages passthrough and opaque account calls.
No second listener, HTTP implementation or credential store was introduced.
The existing reqwest/Hyper APIs provide streaming bodies and raw upgrades:
[reqwest Body](https://docs.rs/reqwest/0.12.28/reqwest/struct.Body.html),
[HTTP upgrades](https://docs.rs/hyper/latest/hyper/upgrade/index.html).

The relay preserves methods, query strings, upload/download frames, HTTP
trailers, encoding, status codes, redirects, cookies and repeated headers.
Hop-by-hop fields, including fields named by Connection, are filtered; the
proxy's own credentials are consumed locally. Account authorization is preserved.
WebSocket negotiation retains subprotocol/auth headers, then tunnels raw bytes
bidirectionally. Incoming Host/absolute-form request targets cannot change the
fixed configured Anthropic origin. Requests are not retried by application code.

The [launcher](../../scripts/claude-account-router) retains its upstream
mitmproxy implementation. `--all-endpoints` maps every API path to an upgraded
router. `--start-router` adds a session-owned router on an unused loopback port,
using the installed binary and existing backend configuration. Both proxy
children stop when the client exits; other router processes are untouched.
The default mode keeps compatibility with older servers. Existing access-only
snapshot, full-login and isolated-configuration constraints still apply.

## Native browser tool compatibility

Live browser testing revealed two independent compatibility problems:

- ToolSearch `tool_reference` results were replaced with unsupported-block text.
  The translation now retains discovered tool names, whose schemas are already
  registered in the request's tools. [request.rs](../../src/providers/codex/translate/request.rs)
  now renders those names without fabricating definitions.
- Claude Code 2.1.302 enables Message Threads on the first-party-origin path.
  A `thread:create` includes history; `thread:continue` includes only the delta.
  Passing that delta to Codex loses the task and yields orphan function outputs.
  [server.rs](../../src/server.rs) now negotiates a full-history resend before
  invoking stateless providers. The capability on [Provider](../../src/provider.rs)
  leaves Anthropic threads and token counting unchanged. No thread store was added.

The resend protocol was verified against upstream's
[merged implementation](https://github.com/KarpelesLab/teamclaude/pull/378):
HTTP 400, `x-should-retry: false`, and
`error.details.error_code: thread_unsupported_request`. Integrating that response
in the existing Rust routing owner avoids introducing a separate fleet proxy or
duplicating conversation persistence. A temporary attempt to flatten orphan
results was removed once wire inspection established the actual thread cause.

Regression coverage exercises both regular Responses and Responses Lite.

## Validation

[account_endpoints.rs](../../tests/account_endpoints.rs) uses real local
upstreams for account methods/paths, multipart bodies beyond the JSON body
limit, error/redirect/HEAD semantics, duplicate cookies, compressed bodies,
inbound authentication, proxy-key stripping, fixed-origin routing, SSE,
streaming uploads, trailers in both directions, and WebSocket handshake/frames.
The launcher suite exercises both routing modes and starts/stops an actual
session-owned model router against a toy account upstream.

A fresh live all-endpoint session on Claude Code 2.1.302 registered Remote
Control, returned `ACCOUNT_RELAY_FINAL_OK` from GPT, and fetched actual
subscription usage through the new relay. A separate ten-turn native Chrome
run discovered tools, selected the local browser, created a new tab, navigated
to example.com, successfully called `read_page`, and closed its own tab; the
browser reported the empty test group removed. Normal browser permission
prompts remained in effect: one navigation and a later deeper read timed out
waiting for permission, while the retried navigation and first page read worked.
The terminal result alone was not used as evidence; tool results were inspected.
No replacement Chrome MCP was installed. Source Claude configuration and the
preexisting router on port 18765 were not modified or restarted.

Final verification: `cargo test --locked` passed 1,047 tests, including eleven
account transport tests; `cargo clippy --locked --all-targets` passed with two
preexisting warnings in Gemini and continuation-test code; `cargo fmt --check`
passed. All ten launcher tests, Ruff lint and Ruff format checks passed.
`cargo build --release --locked` succeeded, and the release executable was
installed by atomic replacement and verified byte-identical to the build.

This validates Remote Control registration, not a phone-originated turn.
Artifact/file HTTP paths and payloads have local transport coverage; no artifact
was published to the user's account as part of testing. The generic relay cannot
override client-side eligibility checks, organization policy, browser permissions,
or access-token expiry. The origin-preserving launcher and a valid full login
remain necessary for account features. Live diagnostic transcripts, screenshots,
and account selectors stay outside version control.

Availability-sensitive model listing tests now assert the registry's actual
visibility decision instead of depending on a locally running optional backend.
Global CCP_SHOW_ALL_MODELS is not required for the full suite, because CLI
visibility tests deliberately verify hidden signed-out providers.
