# T3 fleet attachment

The existing official T3 0.1.8 local ACP provider lists and imports native sessions.
Its `ws.acpRegistry.importSession` maps the native ID deterministically, binds the
selected T3 project's root, and starts in `approval-required` mode. The bridge
reuses this flow without changing T3 or launching another Claude worker.

`scripts/cc-proxy-acp` uses the official Python ACP SDK. Fleet discovery reuses
MCP `list_sessions`: the root HQ's unfiltered REST snapshot contained 1,573 retained
rows (2.37 MB), while MCP returned 26 live rows. Selected history and actions use
the existing root HQ API. Opaque IDs are retained, including separate native/tmux
entries when there is no authoritative alias.

ACP protocol 1 is deliberate: stock T3 can treat an early idle notification from
an ACP v2 attachment as completion before the native reply arrives. The bridge's
synchronous prompt waits for its own native user boundary, a reply, and confirmed
idle; text chunks carry suffix growth instead of replaying whole messages.

One observer polls only its selected session. Message updates preserve HQ
identity; stable transcript IDs take precedence over the legacy content/overlap
fallback. Unidentified native token previews are excluded to avoid duplicate
persisted history after reload. Teardown cancels only the observer. Explicit ACP
cancellation sends an HQ interrupt, while HQ owns input validation, revision
checks and final action delivery. Uncertain writes are never retried.

The deployed T3 is official `0.1.8` in the existing `t3code.service` on dev, with
data under `/home/service/.t3`. Provider `cc_fleet` remains beside the existing
`claude_routed` provider and uses project `CC fleet` at
`/home/service/Projects/cc-fleet`. Stable adapter releases live under
`/home/service/.local/share/cc-proxy-acp/releases/<sha256>/`; the configured versioned
`run` wrapper invokes `uv run --script`. Settings support atomic reloads, so adapter
updates do not require restarting T3 or the original Claude workers. The convenience
launcher is `/home/service/.local/bin/cc-proxy-acp`.

The T3 command's MCP/HQ URL and token-file arguments are private provider
configuration; they are not Claude.ai connector settings. Root HQ's existing
private tailnet listener has an empty `CC_TMUX_TOKEN`; its hub-token file contains
only a newline. The bridge omits an empty Authorization header and preserves that
existing private boundary. Its separately required public MCP token stays in a
mode-0600 file. Public cc-proxy MCP verifies Authentik OAuth or existing machine
bearer credentials. No listener, tunnel, SSO rule or credential policy was changed
for T3.

Official T3 0.1.8 imports the route without automatically loading prior history.
The first T3 prompt resumes the existing session, and that turn's prompt/reply are
persisted normally. Historical and passive external messages remain in the ACP
adapter snapshot; stock T3 does not display or persist them. Its Stop control
requires a T3 turn. The generic local ACP provider sends `session/cancel` and waits
for actual completion; disconnecting only detaches. Use the existing fleet MCP/HQ
for work started externally and full history. A local T3 patch was evaluated and
then removed from the live service at the user's request; no T3 fork is required
or maintained.

The upstream [native-history import fix](https://github.com/pingdotgg/t3code/pull/22155)
addresses initial history in newer official builds. Its accepted scope explicitly
excludes ongoing output generated outside a T3 turn. The existing stable release
remains installed; no nightly update was performed.

Official 0.1.8 was restored with T3's supported updater after verifying the upstream
archive checksum. The consistent database backup passed SQLite `quick_check`;
data and provider settings were retained. Backup locations are
`/home/service/.local/state/cc-proxy/t3-before-official-restore-20261115` and
`/home/service/.local/state/cc-proxy/t3-official-rollback-20261115`. The experimental
source remains isolated on local branch `fix/acp-import-history`; it was not pushed
upstream. No schema migrations were introduced.

The existing SSH `dev` host persists its loopback 3773 forward alongside the
previous forwards. T3 remains private at `http://127.0.0.1:3773` while that existing
connection is active. No public T3 listener or extra daemon was added.

The 44 bridge tests cover real SDK stdio negotiation, safe discovery/load, exact
opaque routes, growing message identity, concurrent prompts, stale revisions,
uncertain delivery, approval identity, user questions and cancel versus detach.
Ruff passes. Run with:

```sh
uv run --no-project --with agent-client-protocol==1.0.0 --with mcp==2.3.0 \
  --with httpx==0.28.1 --with pytest pytest -q tests/account_acp_test.py
uvx ruff check scripts/cc-proxy-acp tests/account_acp_test.py
```

Live read checks returned 26 fleet rows and the selected toy's native history.
T3's native getConfig RPC reported the provider ready and native session list,
load and resume enabled, with delete disabled. The UI listed all 26 rows and
imported the existing toy tmux route without sending a prompt.

Final stock acceptance used an isolated session started through the existing
`tcc`/`claude-accounts` wrapper. The installed bridge negotiated protocol 1; its
live process used release `629a4e8a`. Official T3's normal WebSocket command and
subscription APIs imported the selected route, persisted exactly one user marker
and one assistant reply, and replayed the assistant update through its subscription.
A single Stop interrupted a later busy response, marked the T3 run interrupted,
and returned the original native CLI to its idle prompt. Claude PID `3528213`
and process start tick `18791571` remained unchanged. Temporary acceptance access
tokens were revoked after each test. No test prompt or Stop was retried.
