# Session Mod transport through the existing proxy

For new interactive `claude-accounts` launches, merge this field into the existing
private accounts configuration:

```json
{"session_mod": true}
```

The launcher loads the bundled Mod and enables native Remote Control. The Mod
uses the launcher's existing `HTTPS_PROXY`, CA trust and optional wstunnel. Its
fixed destination is
`https://session-events.cc-proxy.invalid/v1/code/sessions/mod-events`.
No native HQ key or account credential is given to this transport. The `.invalid`
host cannot resolve into a real upstream if the selected proxy lacks this feature.
Existing direct `{ "url": ..., "key_file": ... }` configuration remains available
for a local HQ or explicit HTTPS deployment; see `scripts/session-mod/README.md`.

## Owner and authentication boundary

The existing `account_remote_route.py` mitmproxy addon alone forwards these batches.
It accepts only the regular forward-proxy mode, exact HTTPS host/port/path, POST,
and a `native_session_id` already registered through that proxy. Reverse API
listeners and other proxy modes cannot use this route, even with an API key.
A previous authentication rejection is preserved. The addon discards all caller
headers and supplies its existing HQ native launch key only on the configured
HQ hop. HQ still validates identity, producer ordering, receipts and observations.

This reuses the regular proxy's existing private network or authenticated tunnel
access boundary. It does not add per-client authentication to a proxy that has
none. Keep that listener private. No new listener, credential issuer, native
command path or session registry is added. The existing proxy traffic logging
policy still applies.

A proxy without a native HQ backend returns a local 503. It never guesses another
owner or sends observations to Anthropic. The Mod may buffer temporarily; inspect
`/cc-session-status`. This feature cannot make a standby proxy's native routing
configuration equivalent to the primary. Configure native ownership separately
before expecting observations through a standby.

## Existing-process rollout

The launcher opt-in affects only new interactive children. It must not reload,
interrupt or restart existing Claude sessions. Print, bare, safe-mode and explicitly
disabled Remote Control launches remain unmodified, with inherited Mod event
configuration removed. Interactive configuration errors remain explicit.

For a proxy already loading `account_remote_route.py`, mitmproxy's existing script
watcher can load an atomic replacement of that one file. The addon reload reads
the configured persisted native session map, and its `running()` hook adds only
the reserved host to a restricted interception allowlist. An empty allowlist stays
unrestricted. Native registration and worker response metadata retain their existing
shape; the event path never rewrites the session map.

Validate the exact installed mitmproxy version and a private canary first. Preserve
its configured HQ key, native map, CA, grant owner and command arguments. Updating
launcher source is separate from changing a running shared service. No running
process needs to follow the Git branch or restart merely because source is merged.
