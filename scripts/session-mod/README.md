# Interactive session events

This official Claude Code Mod publishes additional structured events to the
existing HQ native-session owner. Claude Code remains interactive; the existing
native Remote Control lane still owns messages, interruption, and permission
answers. It creates no listener, session registry, transcript copy, or worker.

Configure `session_mod` once in the launcher's existing private accounts file:

```json
{
  "session_mod": true
}
```

Merge that field into the existing account configuration. The launcher requires
its existing HTTPS proxy route. Events go to a reserved `.invalid` address that
the proxy intercepts and forwards to its existing HQ native-session owner. The
client reads and sends no HQ credential. Only already routed native sessions
are accepted; a proxy without the HQ backend refuses publication locally.
See [transport boundaries](../../docs/session-mod-transport.md).

For a launcher on the HQ owner itself, direct configuration is also supported:
`"session_mod": {"url": "http://127.0.0.1:8389", "key_file": "/private/path/to/existing-native-launch-key"}`.
That URL must be HTTPS or loopback HTTP, and the existing key file must be private.
The launcher supplies the plugin directory and configuration to new interactive
children automatically. No key is copied or included in command-line arguments.
Existing sessions are not reloaded or restarted. Missing configuration disables
the Mod; print/SDK/safe/bare launches continue without it.

The HQ needs the `mod-events` and observer API implementation. Claude's native
wire ID differs from its conversation UUID. The Mod reads only its own
`CLAUDE_CONFIG_DIR/sessions/<CLAUDE_PID>.json` publisher and requires the exact
current UUID/PID before attaching that pair to an already registered native
session. A 404 buffers events while Remote Control registers. UUID changes use
the previous UUID as a compare-and-set guard; stale producers cannot rebind it.
It never creates or guesses a session from a title or working directory.
Use `/cc-session-status` to inspect connection state without starting a model turn.

Consumers use the shared [SessionClient](../../docs/session-sdk.md). Events include
prompts, turn starts/completions, visible stream chunks, tool inputs/results, and
transcript append metadata. The Mod passes native inputs, results, and stream
chunks through unchanged and never grants a tool permission. Native and Mod
observations retain distinct sources; they are not two independent transcripts.

The in-memory publisher is bounded to 256 queued records and a conservative
128 KiB estimate. Oversized events and overflow produce `gap` records. A changed
conversation UUID starts a new publisher. An already attempted immutable batch
settles before the new identity binds; unsent old observations and late tool
results are counted as gaps, never attributed to the new conversation. Reload
or process exit can discard unacknowledged memory; this is an observation feed,
not lossless transcript storage. Consult the native transcript for history.

Event retries reuse an immutable batch and monotonically increasing IDs scoped
to one Mod load. HQ deduplicates those IDs. Invalid credentials, identity
receipts, and permanent protocol failures stop publication. A network failure
never retries a tool or a user command. Slow networking does not block a tool or
model stream. The session's normal managed permission and computer-use consent
boundaries remain in force.

## Actual serving model

For the band without HQ event publishing, merge `"model_metadata": true` into the
existing private accounts file. `session_mod` may remain false or absent: this
loads the same Mod but sets no event destination, starts no publishing timer,
and does not enable Remote Control. Print/bare/safe/disabled launches stay unchanged.
Existing `session_mod: true` users already get the band without another setting.

The compact AbovePrompt band separates the requested model from the proxy's
`x-claude-codex-served-by` response header. It reads response headers through the
native `http.fetch` hook; the streaming body stays with the engine. Model/account
changes produce one quiet toast, not model text or a transcript annotation.
Account emails use stable local pseudonyms. No credential or pool header is read.

Only an active main `turn.step` and a matching tool-bearing inference request
can establish identity. Agent/parent-agent header presence excludes subagents;
simultaneous same-model agents make attribution ambiguous. Title, count and
tool-less requests do not establish identity. Public HTTP hooks expose no turn ID,
and `turn.step.messageCount` is not the serialized wire message count: when
correlation is absent or ambiguous the band is hidden rather than guessed.
A missing or unparseable header, a pending request, an error, a model change, and a
reload/resume all hide the band until the next attributed answer.
The band names the served model once (`Serving <model> · <provider/account>`) when it
is the requested one; aliases (`sol`, `opus`), `[1m]` hints, Codex `-fast` tiers, dated
Claude ids, `kimi-` and route prefixes (`gemini:`, `cursor-cli-ask:`) count as the same
model. Anything else is prefixed with ⚠️ and names the requested model, and only such a
real model/account change toasts. Metadata works independently of the HQ event
connection and adds no poller or listener.

The native inference hook and band were verified interactively on Claude Code
2.1.331/2.1.332. Builds that do not expose inference HTTP hooks show no band;
the requested model is never shown as served.
The normal launcher opt-in above installs the band only for new interactive
sessions. Development `--plugin-dir` reloads are tested only in throwaway sessions.

The footprint is `session.start`, `command.run`, `prompt.submit`, `turn.start`,
`turn.step`, `tool.call`, `session.append`, `turn.complete`, `session.end`,
`classic.PostModelSwitch`, `http.fetch`, and `ui.render` (AbovePrompt).
Calls read the two named event environment variables and `CLAUDE_CONFIG_DIR`,
`session.id`, and the engine's `process.hookEnv` identity. They register the
status command/timer, read the exact process publisher and the configured key
only in direct mode, and POST to the configured endpoint. `hookEnv` may create the engine-owned plugin
data directory. The Mod invokes no model or shell, submits no prompt, overrides
no permissions, and writes no session data.

Development uses `claude plugin validate --strict`, `claude plugin test`, and
the declarations generated by the exact CLI on an isolated interactive load.
The current implementation uses the official
[Mods events](https://code.claude.com/docs/en/plugins/mods/events) and
[API](https://code.claude.com/docs/en/plugins/mods/api).
The community [session-bridge](https://github.com/tsurutanmen/session-bridge)
was evaluated; its independent filesystem registry/inbox/poller is not adopted.
