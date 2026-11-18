# Interactive session SDK

`cc_proxy.session_client.SessionClient` programs an existing interactive session
through the existing fleet HQ. The Claude terminal keeps its native tools,
permissions, browser connection, and process identity. The SDK does not launch a
replacement agent or own another session registry. `cc-proxy-acp` uses this same
client for its HQ reads and actions.

Install `cc-proxy[sessions]` for private HTTP access, `cc-proxy[sessions-mcp]` for
optional compact MCP discovery, or `cc-proxy[acp]` for the ACP adapter dependencies.
These are optional extras; the existing proxy's default dependencies are unchanged.
The SDK imports no ACP classes and needs only `httpx` when MCP is not configured.
Use the existing private HQ URL and credential. It does not add a login, pairing,
or browser OAuth flow. Give MCP its own existing credential if using that origin.

```python
import asyncio
import os
from pathlib import Path

from cc_proxy.session_client import SessionClient

async def main():
    token_file = os.environ.get("CC_TMUX_HQ_TOKEN_FILE")
    token = Path(token_file).expanduser().read_text().strip() if token_file else ""
    async with SessionClient(os.environ["CC_TMUX_HQ_URL"], token) as sessions:
        # Discovery is read-only. Select the exact opaque ID, never a guessed name.
        for row in await sessions.list():
            print(row["id"], row.get("title"), row.get("host"))
        session_id = os.environ["CC_SESSION_ID"]
        detail = await sessions.detail(session_id)
        print(detail.get("messages", []))
        # Explicit writes, when intended:
        # receipt = await sessions.send(session_id, "Explain the current change.")
        # await sessions.interrupt(session_id)

asyncio.run(main())
```

For a source checkout without installing the proxy package, run the example with
`PYTHONPATH=python uv run --no-project --with httpx==0.28.1 example.py` from its root.
Do not print the token. HTTP requests never follow redirects, and the HQ token is
never forwarded to an MCP origin. MCP discovery is enabled only by explicitly
passing `mcp_url=...` and `mcp_token=...` to `SessionClient`.

## Actions and delivery

`send`, `interrupt`, `respond`, `set_model`, `set_permission_mode`, and the generic
`action` method delegate to HQ's current-revision check and receipt owner. Model
and permission-mode changes require support from the selected native owner.
`respond(session_id, response, revision=detail["revision"])` requires the exact
native request payload and inspected revision; it never automatically approves a
request. The SDK never guesses an equivalent native/tmux route.

An action receipt means the owner accepted or processed that command, not that an
assistant turn has completed. Observe the source state or transcript for completion.
A verified stale revision may be refreshed at most twice before returning an
error; an explicitly supplied revision is never refreshed automatically. A stale
revision retry also preserves the first observed canonical Claude conversation UUID;
if that UUID changes, the original command is rejected before another write.

- `ActionRejected` means the command was not sent or was definitively rejected.
- `DeliveryUnknown` means the write may have reached the original session. Its
  `request_id` identifies that attempt. Inspect the session/receipt before deciding
  what to do; do not resend automatically, including after cancellation of a send.
- `SessionClientError` reports a read, configuration, or protocol problem.

Exiting the context or calling `aclose()` closes local connections only. It does
not interrupt, archive, end, or restart the worker. Cancelling an observation also
has no remote action. There is no unattended fleet census or background observer.

## Native and Mod observations

`read_events(session_id, after=None, wait=0)` returns an `EventBatch`;
`events(session_id, after=None, wait=20, poll_interval=1)` is an async iterator of
`SessionEvent` objects. Each event has `session_id`, an opaque `cursor`, `source`
(`native` or `mod`), `kind`, original JSON `data`, and optional producer `id`.
Native events come from the existing HQ ingest owner; an optional Claude Mod adds
observations from inside the same terminal. Neither source is a command queue.

```python
from cc_proxy.session_client import ObservationGap

async def observe(sessions, session_id, last_consumed=None):
    try:
        async for event in sessions.events(session_id, after=last_consumed):
            consume(event)  # your handler; do not execute arbitrary event payloads
            last_consumed = event.cursor  # persist only after processing succeeds
    except ObservationGap as gap:
        detail = await sessions.detail(session_id)
        report_gap(gap.reason, detail)
        # Explicit policy decision: resume with after=gap.resume_cursor if acceptable.
        # Missing observations cannot be reconstructed by replaying commands.
    return last_consumed
```

With no cursor, HQ returns its bounded retained tail, not complete history. Read
`detail()` for available transcript history. A lost or restarted journal produces
`ObservationGap` with the requested and new resume cursors; the SDK never silently
skips that gap. Read/network failures propagate so callers can resume explicitly
from their last consumed cursor. Duplicate handling across application restarts
belongs to the consumer; the source event/producer identities remain unchanged.

Long polls wait at most 20 seconds. `poll_interval` (0.05–60 seconds) prevents an
older/immediate-response server from causing a tight loop on empty reads. Current
observer support is native Claude Remote Control; unsupported tmux/Codex routes
return an error rather than inventing a stream. Existing detail/actions retain
their existing fleet coverage. No use is made of the CLI's worker command SSE.

## Packaging the ACP adapter

The SDK's single implementation lives at `python/cc_proxy/session_client.py`.
Wheels already package `cc_proxy`; `scripts/cc-proxy-acp` imports it. For immutable
standalone ACP bundles, include **both** the script and matching package source:

```text
release/
  scripts/cc-proxy-acp
  python/cc_proxy/__init__.py
  python/cc_proxy/session_client.py
```

Point the existing command wrapper at `release/scripts/cc-proxy-acp`, keeping its
current arguments. The script's PEP 723 dependency pins still support `uv run
--script`. A copied one-file script alone is no longer a complete new release.
No existing deployed adapter is replaced by this source change.
