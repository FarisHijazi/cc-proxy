# T3 fleet attachment

The existing T3 0.1.8 local ACP provider already lists and imports native sessions.
Its `ws.acpRegistry.importSession` maps the native ID deterministically, binds the
selected T3 project's root, and starts in `approval-required` mode. Reusing it avoids
a T3 fork or a second session manager. Upstream transcript import for Claude itself
would start another provider process and cannot attach the live fleet worker.

`scripts/cc-proxy-acp` uses the official Python ACP v2 SDK. The v2 full-message and
state updates preserve stable HQ message identity and let T3 observe work that was
already running. One observer polls only its selected session. Its teardown cancels
the observer; only explicit ACP cancellation sends an HQ interrupt. The existing
HQ owns all input validation, revision checks and final action delivery.

Fleet discovery reuses MCP `list_sessions`: the root HQ's unfiltered REST snapshot
contained 1,573 retained rows (2.37 MB), while MCP returned 26 live rows. Selected
history and actions use the existing root HQ API. Opaque IDs are retained, including
separate native/tmux entries when there is no authoritative alias. Stable transcript
message IDs take precedence over the legacy content/overlap fallback. Unidentified
native token previews are excluded to avoid duplicate persisted history after reload.

The deployed T3 process remains the existing `t3code.service` on dev, with data under
`/home/service/.t3`. Its settings service supports atomic file reloads. Provider
`cc_fleet` is configured beside the existing `claude_routed` provider; it uses project
`CC fleet` at `/home/service/Projects/cc-fleet`. Stable adapter releases live under
`/home/service/.local/share/cc-proxy-acp/releases/<sha256>/`, and the configured
versioned `run` wrapper invokes `uv run --script`. Updating only that provider's
command path reloads its adapter without restarting T3 or the original Claude workers.
The convenience launcher is `/home/service/.local/bin/cc-proxy-acp`.

Root HQ's existing private tailnet listener has an empty `CC_TMUX_TOKEN`; its
`hub-token` file contains only a newline. The bridge omits an empty Authorization
header and preserves that existing private boundary. Its separately required public
MCP token stays in a mode-0600 file. Public cc-proxy MCP still verifies Authentik
OAuth or existing machine bearer credentials. No listener, tunnel, SSO rule or
credential policy was changed for T3.

Verification covers real SDK stdio negotiation, safe discovery and load, exact
opaque routes, growing message identity and reloads, concurrent prompts, stale
revisions, uncertain delivery, approval identity, user questions, and explicit
cancel versus detach. Run with:

```sh
uv run --no-project --with agent-client-protocol==1.0.0 --with mcp==2.3.0 \
  --with httpx==0.28.1 --with pytest pytest -q tests/account_acp_test.py
uvx ruff check scripts/cc-proxy-acp tests/account_acp_test.py
```

Live read checks returned 26 fleet rows and the selected toy's native history.
T3's native getConfig RPC reported the provider ready and native session list,
load and resume enabled, with delete disabled. The UI listed all 26 rows and
imported the existing toy tmux route without sending a prompt. The original T3
service PID did not change.
