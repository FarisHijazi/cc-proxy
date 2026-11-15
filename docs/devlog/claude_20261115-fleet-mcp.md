# Fleet MCP through cc-proxy

The existing cc-proxy dashboard listener gains an optional `--mcp-upstream` origin.
The relay forwards MCP and exact OAuth discovery routes to the already running
HQ adapter. It adds no process, node registry, poller or terminal writer.
Claude-account sessions continue through HQ's existing discovery/native transport.
The deployed adapter remains the OAuth verifier; deployment instructions are in
README's "Fleet MCP for Claude.ai" section.

Validation on the dev VM: `uv run --no-project --with pytest pytest -q
tests/account_mcp_relay_test.py tests/account_ui_test.py` passed 65 tests, and Ruff
passed for the new module/tests. Coverage includes credential/header isolation,
transport methods, unchanged dashboard routes, bounded requests, live chunked SSE
forwarding, discovery redirects and no retry after a dropped action response.

Deployment on ftower uses the existing `cc-proxy-ui.service` on port 18792 and sole
MCP adapter on port 8392. Its additional `zz-fleet-mcp.conf` drop-in preserves every
existing argument and adds `--mcp-upstream http://172.17.0.1:8392`. Only the UI service
was restarted. Exact MCP and discovery routes on cc-proxy and legacy cchq both
forward through this UI; all dashboard/API routes retain Authentik forward-auth.
Original scripts/configs are in `~/.local/state/cc-proxy/mcp-deploy-20261115`, outside
Traefik's watched directory. Rollback restores those files, removes only the new
UI drop-in and relay, reloads systemd user units, then restarts only the UI.

Public transport verification initialized MCP, listed eight tools and 26 active
sessions across five session sources, read a transcript with a revision, and listed
node health. Anonymous calls returned 401 with canonical cc-proxy protected-resource
metadata. Authorization discovery returned the fixed Authentik issuer redirect,
whose metadata contained the correct authorization endpoint. Dashboard and unknown
MCP subpaths still redirected through Authentik. Browser OAuth sign-in is verified
separately by the coordinating task; bearer smoke checks alone do not prove it.
