# Account features with inference routing

## Decision

Current Claude Code rejects Remote Control for a custom `ANTHROPIC_BASE_URL`:
[official requirements](https://code.claude.com/docs/en/remote-control).
Installed version 2.1.302 also explicitly excludes the internal
`_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL` flag from that eligibility check.
An account passthrough route in our server cannot change this client decision.

The community already uses an HTTPS forward proxy to retain the first-party
origin, for example
[claude-code-cache-fix forward mode](https://github.com/cnighswonger/claude-code-cache-fix#forward-proxy-mode-keeps-remote-control-working).
That cache-repair proxy does not provide our model router's inference-only
split. Rather than implement HTTP CONNECT or TLS ourselves,
[`scripts/claude-account-router`](../../scripts/claude-account-router) delegates
to unmodified mitmproxy 12.2.3 and its
[built-in Map Remote feature](https://docs.mitmproxy.org/stable/overview/features/#map-remote).
Usage and prerequisites are in [README.md](../../README.md#keeping-account-features-while-routing-inference).

## Traffic and credentials

The client sees `https://api.anthropic.com`. A separate loopback listener
intercepts only that hostname; other HTTPS hosts are blind-tunnelled. Only exact
POST inference and token-counting paths are rewritten to the existing router.
Queries, request bytes and authentication headers are preserved. Other paths,
including OAuth usage, Remote Control and artifacts endpoints, retain their
original upstream. The router still owns model selection in `src/server.rs`.
The model router destination is restricted to loopback: Anthropic credentials
must not be sent to an arbitrary gateway.

CLI settings override any existing base-URL and credential environment settings
for this session. A separate `CLAUDE_CONFIG_DIR` holds account metadata and
transcripts; a unique temporary `CLAUDE_SECURESTORAGE_CONFIG_DIR` holds the
access-only credential. Source hooks/plugins are not copied, and hooks are
disabled for the isolated client. No source config file or system trust store
is modified. A process-scoped CA bundle retains an existing
`NODE_EXTRA_CA_CERTS` bundle, when present. Flow captures are disabled.

The macOS Keychain is read before plaintext fallback. Full login scopes are
preserved, but the source's refresh grant is deliberately excluded to avoid
competing refresh-token rotation. The temporary credential is removed on exit.
Relaunching reads the source's current access token; an expired saved token
needs renewing in its source account. Setup-token credentials have inference
scope only and cannot enable account features.

Optional `--cswap-account` reuses `claude_swap.credentials.CredentialStore`'s
read method and saved account metadata. It never constructs the switcher or
switches, refreshes or writes the saved account. It requires `claude-swap` in
the launching Python environment, verifies profile identity, and detects a
changed account sequence during snapshotting. Different simultaneous accounts
should use separate `--state-dir` values to keep transcripts and settings apart.

## Streaming trap and upstream attribution

`--set stream_large_bodies=1` streams requests before mitmproxy's `map_remote`
request hook runs, sending inference to the original server before routing.
Requests must remain buffered; only responses stream. We vendor the unmodified
[response-streaming example from mitmproxy v12.2.3](https://github.com/mitmproxy/mitmproxy/blob/v12.2.3/examples/addons/http-stream-simple.py)
as [`scripts/account_router_stream.py`](../../scripts/account_router_stream.py),
with its [MIT license](../../scripts/LICENSE.mitmproxy). Do not replace it with
the bidirectional streaming option. The integration test proves a first SSE
chunk arrives while the mock server is still holding the final chunk.

## Validation

`uvx pytest tests/account_router_test.py`: 8 passing tests covering route/method
boundaries, live TLS interception and SSE streaming, toy authorization/body
preservation, access-only snapshots, unchanged source credentials, Keychain
precedence, account metadata changes, inherited auth overrides, symlink guards
and read-only switcher access. Ruff lint and format checks pass.

With Claude Code 2.1.302 and an existing full-scope saved login, a real isolated
session simultaneously showed Claude Max, `gpt-5.6-sol`, and Remote Control
active. A model request returned the requested exact marker through the active
router on port 18765. `/usage` fetched subscription utilization and reset times.
`/chrome` reported Enabled, Extension Installed, and discovered three connected
browsers. These tests used no source-profile writes and did not stop or change
the active router.

Native browser page actions and artifact publishing were not exercised. Their
account endpoints retain the original routing, but successful publishing is not
claimed. The default setup-token profile correctly failed the full-scope
Remote Control requirement. Interactive `--remote-control` is supported;
Claude's `remote-control` server subcommand rejects preceding CLI settings and
is rejected explicitly by this launcher.
