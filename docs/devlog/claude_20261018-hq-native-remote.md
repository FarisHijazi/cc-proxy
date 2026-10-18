# Route native Remote Control to HQ

## Plan and ownership

Extend the existing [account launcher](../../scripts/claude-account-router) and
mitmproxy listener with two options: `--remote-control-backend` and
`--remote-control-key-file`. HQ's `cc-tmux-api` project owns the emulated native
protocol and the existing web UI. This repository owns only request routing;
neither a second inference router nor a terminal control service is added.
The protocol/backend record is `cc-tmux-api/docs/devlog/claude_20261018-native-remote-control.md`.

## Boundary

The [addon](../../scripts/account_remote_route.py) records the original API path
before mitmproxy's existing model/account mapping. Only bridge registrations at
`POST /v1/code/sessions` are selected. Successful responses establish a private
set of session IDs owned by this proxy instance. Only those IDs subsequently
route to HQ. Unknown sessions, native Chrome and unrelated account endpoints
retain their existing route.

Before forwarding, remove OAuth Authorization, API key and cookie headers.
Registration/bridge/archive requests use a separate HQ launch key read from a
private file. Worker calls must carry the exact token issued by that session's
bridge response. The key value never appears in process arguments. Origin
configuration permits HTTPS or loopback HTTP and rejects embedded credentials,
paths and query strings. Buffer registration/bridge responses to learn IDs and
tokens; leave worker event streams unbuffered. Session-owned child cleanup and
access-only login snapshots remain the launcher's existing responsibility.

The installed production Claude 2.1.302 compiles its bridge-base override
helpers to no-ops. A session-specific proxy is therefore used; global hosts,
trust stores and Claude configuration are untouched. This feature replaces the
Anthropic Remote Control service for these new sessions, so the native printed
claude.ai URL cannot open them. Use HQ instead. Existing processes cannot have
their launch environment changed retroactively.

## Checks

Seven addon tests cover registration after map_remote, stripping account secrets,
issued-token isolation, streaming, unknown session/account passthrough and host
lookalikes. Run in a separate mitmproxy environment:

```sh
uv run --with mitmproxy==12.2.3 --with pytest pytest -q tests/account_remote_route_test.py
```

A real interactive CLI registered with the paired HQ backend, reported Remote
Control active, and exchanged a browser-origin prompt and structured Blue answer.
Its terminal independently showed that answer and a later browser interrupt.
The first test used an older running inference router and failed the post-tool
continuation; it is not proof of end-to-end model completion. A new session-owned
router is used for subsequent verification. Full evidence lives in HQ's devlog.

The fresh session-owned router passed the complete real-browser question flow:
prompt, `HQ_NATIVE_OK`, Blue answer and completed `HQ_ANSWER Blue`. Real native
Write approval and denial also passed, checked against the private filesystem.
After HQ restarted, the CLI reconnected with unchanged history and completed a
new `HQ_RECONNECTED` prompt. No source login directory was modified.

All 36 addon/Chrome-route/launcher tests passed with mitmproxy 12.2.3 and the
existing debug binary made available to the worktree for the launcher integration
test. The temporary binary-path symlink was removed afterward. Rust code did
not change in this worktree.
