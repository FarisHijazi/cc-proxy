# Select a native Chrome account independently of main

## Plan and ownership

Extend the [account launcher](../../scripts/claude-account-router) with
`--chrome-account ALIAS`. Resolve the account through its existing snapshot
owner, then route the built-in Chrome server's bridge through the existing
mitmproxy listener. Keep Remote Control and ordinary account HTTP traffic on main.
Do not replace Claude's reserved MCP server or reproduce its file preprocessing.
This refines the native Chrome limitation in the
[aggregation record](claude_20261018-multi-account-tools.md).

The installed native CLI provides browser selection, permissions and tool calls.
mitmproxy already provides TLS, WebSocket transport and mutable messages through
its [event hooks](https://docs.mitmproxy.org/stable/api/events.html) and
[WebSocket API](https://docs.mitmproxy.org/stable/api/mitmproxy/websocket.html).
Neither supplies this launcher's independent account selector. The new
[addon](../../scripts/account_chrome_route.py) only changes identity at connection
establishment; it does not introduce a second transport or credential writer.

## Routing contract

Claude Code 2.1.302 connects to
`wss://bridge.claudeusercontent.com/chrome/<account_uuid>` and sends an initial
JSON frame with `type=connect`, `client_type=claude-code`, and `oauth_token`.
Only this exact HTTPS host, port, path, GET method and WebSocket upgrade are
selected. Rewrite the source UUID to the target UUID. Validate the initial token
against the main snapshot, then replace it with the selected account's token.
Leave subsequent messages and server notifications opaque. Remote Control's
`/devices/.../bridge` path is not selected.

Both snapshots must be unexpired, access-only full logins with `user:profile`.
Missing credentials, unexpected initial frames or changed identities fail closed.
No source credentials are refreshed or written, and no token is logged. Native
organization checks and extension permissions remain in force. This depends on
the observed native protocol; future CLI changes may require revalidation.

The selector requires a configured account alias and rejects `--no-chrome`.
When selected, omit `account-chrome` to avoid redirecting an aggregate main child
into the selected native account. Keep `account-artifacts` and native `--chrome`.
Without the selector, existing aggregation remains unchanged. The native picker
shows one selected account, not a merged account list; change the account by
relaunching. File-upload preprocessing remains native, but uploads were not tested.

## Validation

The existing three account suites pass 39 tests; their optional live aggregation
test is skipped. The new route suite passes 13 tests: narrow endpoint selection,
first-frame rewrite, unchanged action/permission traffic, malformed or mismatched
authentication rejection, and expiry without fallback. Launcher integration uses
the already-built relay binary. Ruff lint/format and diff whitespace checks pass.

Run the environments separately: mitmproxy 12.2.3 and FastMCP 4.1.1 have conflicting
typing-extensions requirements under Python 3.12. Production already runs them
in separate processes/environments.

```sh
uv run --with mitmproxy==12.2.3 --with pytest \
  pytest -q tests/account_chrome_route_test.py
uv run --with fastmcp==4.1.1 --with claude-artifact-cli==0.3.0 --with pytest \
  pytest -q tests/account_router_test.py tests/account_chrome_test.py tests/account_artifacts_test.py
```

A live native MCP test compared direct target browser discovery with an isolated
source carrying a synthetic UUID and invalid toy token. Through the production
addon, that source returned the exact same two browser IDs as the real target.
This exercises both path and authentication replacement against the real bridge.
It does not establish a live redirect between two real accounts: the second
saved account currently contains an inference-only credential. The target used a
read-only snapshot of the current full login; no refresh grant was copied.

A separate interactive Claude Code 2.1.302 session launched with explicit main
and Chrome aliases, GPT model selection, and a private router. Native `/chrome`
showed Enabled, Extension Installed, and two connected browsers (Linux/macOS).
Remote Control reported active simultaneously. Those two aliases used the same
usable real login; the synthetic-source test above separately proves identity
redirection. No browser navigation, uploads or remote-origin turns were attempted.
The session was exited and its child processes cleaned up. Existing user routers
and `~/.claude` were not modified by this work.
