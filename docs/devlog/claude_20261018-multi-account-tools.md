# Account aggregation and main account selection

## Plan and ownership

Extend the [existing account launcher](../../scripts/claude-account-router):
resolve an alias-to-source map, select one main account, snapshot each usable
login without refresh grants, then register account-aware MCP adapters. The
main account owns Remote Control, native account operations, and its isolated
transcript directory. Selection does not grant another account access to that
Remote Control session.

Reuse one installed native Claude Chrome MCP process per account, preserving
its browser selection and permission state. Merge browser discovery and route
selected-device calls back to their owning process. Keep native tool suffixes
on a separate account-chrome MCP server. Reuse the published artifact CLI
for reading artifact metadata/files instead of reproducing its transport.

The existing relay and model routing remain the owners of HTTP proxying and
inference; see [the relay record](claude_20261018-account-endpoint-relay.md).
Credential source reads remain in the launcher and claude-swap's CredentialStore.
Account snapshots and manifests are temporary, private files. Failed secondary
sources must produce an explicit unavailable status, never an empty success.

## Upstream research

Native Chrome uses a separate account-addressed WebSocket bridge, established
by `claude --claude-in-chrome-mcp`. HTTP endpoint forwarding alone cannot merge
these connections. The [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
provides the stdio transports and sessions for a thin account dispatch layer.

The native artifacts JSON-RPC endpoint rejects standard MCP initialization and
requires private routing metadata. Rather than recreate its file semantics,
reuse [claude-artifact-cli](https://github.com/nikships/claude-artifact-cli), which
already handles artifact manifests, content hosts and file validation. Neither
it nor the native Chrome server supplies multi-account aggregation.

## Interface

`--account ALIAS=SOURCE` accepts a positive claude-swap slot or absolute login
directory. `--accounts-file` reads `{ "main": "work", "accounts": { "work": 2,
"personal": 1 } }`; repeated CLI entries override matching saved aliases.
`--main-account` overrides the saved selection. Without a selection the first
configured account is main. Account mode replaces the single-account
`--cswap-account` / `--login-from` options. Session state is partitioned under
`<state-dir>/accounts/<main>/`, so changing main does not resume another
account's transcript/config directory.

The launcher registers `account-chrome` and `account-artifacts` through a
private temporary MCP config, preserving other caller MCP configs. Native
`--chrome` remains enabled for main unless explicitly disabled. Installed Claude
rejects `claude-in-chrome` in supplied MCP configs as a reserved name, including
when `--no-chrome` is passed. Therefore `/chrome` remains main-account-only;
the aggregate browser list/selection is available through account-chrome tools.
Native file_upload and uploads inside browser_batch require private CLI-side
file-permission preprocessing. The aggregate adapter explains this in tool
descriptions and rejects such calls (including nested batches) before any action.
Use that account as main for native uploads. Native permission-mode UI updates
apply to main; aggregate children retain native extension permission enforcement.
Chrome retains one native subprocess per account;
device IDs are namespaced by alias, and native permission updates are forwarded.

Artifact access is read-only. `list_artifacts` defaults to all accounts;
`read_artifact` defaults to main unless an account is supplied. File reads use
published `claude-artifact-cli==0.3.0`, with a 2 MiB tool response cap. Metadata
excludes capability tokens, sharing keys and signed URLs. Main's native Artifact
tool remains available for its ordinary operations. Aggregation neither shares
artifacts remotely nor changes the browser's signed-in Claude account.

MCP scripts run through `uv run --script`, pinning `fastmcp==4.1.1` and its MCP
SDK. First launch requires dependency downloads. Native Chrome behavior is
version dependent; discovery and forwarding are validated against installed
Claude Code 2.1.302. [Remote Control documentation](https://code.claude.com/docs/en/remote-control)
ties remote access to the signed-in account; no cross-account sharing is claimed.

Python's urllib ignores NODE_EXTRA_CA_CERTS. The launcher's process-scoped CA
bundle therefore retains platform and ambient trusted roots plus the proxy CA,
and is exported through SSL_CERT_FILE and REQUESTS_CA_BUNDLE too. This keeps
artifact API traffic on the same proxy while retaining ordinary trust for
artifact content hosts. Certificate verification remains enabled.

## Validation

The three Python suites pass 37 automated tests; the opt-in Chrome live test
passes separately. Tests cover account/main defaults, read-only snapshots,
source Keychain path spelling, case-insensitive alias collisions, inherited
identity overrides, real proxy streaming/cleanup, native browser ID collisions,
selection state, tool metadata/images/errors, vendor notifications and child
shutdown, plus artifact ownership, failures, metadata sanitization and file reads.
Ruff lint and formatting checks pass for all six Python files.

A real Claude Code 2.1.302 print session used `gpt-5.6-sol` through a fresh
session-owned router, discovered both MCP adapters, listed two work browsers
and three personal browsers, listed artifacts from both accounts, and read
one artifact's metadata from each. It completed six turns with no permission
denials. An interactive Claude session then selected each account's browser and
successfully called tabs_context_mcp with createIfEmpty=false for each, while
Remote Control stayed active on main. Separate live SDK calls selected browsers
on both accounts and read
published HTML files from each account through the artifact adapter. A live
TLS test reproduced the Python trust failure and verified the combined bundle.

Early combined-session testing exposed the reserved Chrome server name and
Python CA mismatch; both informed the final integration above. Artifact writes,
cross-account Remote Control sharing, and a merged native `/chrome` picker are
not implemented or claimed.

Reproduce automated checks:

```sh
uv run --with fastmcp==4.1.1 --with claude-artifact-cli==0.3.0 --with pytest \
  pytest -q tests/account_router_test.py tests/account_chrome_test.py tests/account_artifacts_test.py
```

For the optional Chrome live test, set `CHROME_ACCOUNT_LIVE_MANIFEST` to a private
launcher-format manifest and run `tests/account_chrome_test.py -k live_native`.
Manifests contain access-only credential directory paths and should not be committed.

Both explicit main selections were exercised in separate interactive sessions:
work showed the Team login and personal showed the Max login, and both reported
Remote Control active with different remote session registrations. The work
session's native `/chrome` dialog showed Enabled and Extension Installed after
aggregate tools had selected and read tab context on both accounts. Remote
phone/browser-origin turns were not exercised. All test sessions were exited;
no existing router was restarted.
