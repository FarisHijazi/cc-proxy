# Interactive Claude session SDK and Mod

The implementation keeps the real interactive Claude process and its existing
native Remote Control command owner. ACP now imports a shared `SessionClient`;
the official Mod adds passive structured observations to HQ's existing registry.
It creates no permanent daemon, registry, command queue, or provider. Fleet stays
discovery/transport; Claude Code remains the engine.

Configuration and contracts live in [the SDK guide](../session-sdk.md) and
[the Mod README](../../scripts/session-mod/README.md). The companion HQ change
adds the bounded observer API, authenticated exact UUID binding, durable event
receipts, and action revision fencing when a conversation changes.

## Verification on 2026-11-18

An isolated Dev fixture used Claude Code 2.1.327, its existing subscription route,
a private tmux socket, and loopback-only test HQ/proxy instances. Native tools
and terminal input ran in the same Claude PID and process birth throughout:

- SDK send produced the exact requested Bash output and final reply, with native
  events and Mod prompt, turn, streaming, tool call/result and append events.
- A disconnected SDK resumed the same cursor with no new events. The prompt
  was delivered once; no uncertain command was automatically resent.
- SDK interrupt stopped a verified `sleep 20` child; native `aborted_tools` and
  the Mod's interrupted tool result were observed, and Claude remained alive.
- A prompt typed in that terminal produced the exact requested reply, observed
  from the SDK. This was a real interactive session, not a headless substitute.
- Binding was automatic from Claude's own PID publisher. The first live attempt
  exposed the difference between the native wire ID and transcript UUID; the
  final path bound the exact pair without manual session registration.
- On the final source, `/clear` changed the conversation UUID while retaining
  the original process and bridge. HQ bound the new UUID automatically, and one
  fresh SDK prompt received its exact reply with native and Mod completion.
- All 32 observer long polls succeeded; the excess read returned 503. An ordinary
  detail read still returned 200 in 5 ms. Anonymous API/native launch requests
  returned 401, and all eight original Dev terminal panes retained identity.

Regression checks: 170 SDK/ACP/link tests; 17 official Mod tests and exact-version
TypeScript declarations; 69 launcher/router tests with two existing optional
skips. The HQ full suite passed 566 tests before its final action-fence change;
all 95 affected native/observer tests passed after that change. Independent
reviews covered receipt identity, uncertain writes, UUID rotation, delayed tool
results, cancellation, long-poll isolation and wheel contents.

## Scope and deployment

This source change has only been loaded in the private fixture. Production HQ,
T3, MCP and user sessions were not replaced or restarted. The saved launcher
opt-in applies to new interactive children; a future rollout must update the
existing HQ owner and matching launcher/package together. Standalone ACP bundles
now require the shared Python module, as documented in the SDK guide.

The canary's automatic permission mode did not exercise an approval dialog.
Chrome, artifacts and computer-use consent were not re-tested by this fixture;
the Mod preserves the native execution/permission path and does not bypass it.
The bounded observer feed is not lossless transcript storage, and a plugin does
not provide interception of every server-side action or OS permission. Codex and
terminal-only transports retain existing detail/action support; this new event
stream currently requires native Claude Remote Control.
