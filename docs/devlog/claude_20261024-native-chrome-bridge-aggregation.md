# Native Chrome bridge aggregation

The shared `--serve` proxy now supports native Chrome aggregation for updated
`--via` clients. The launcher adds `+chromes` to the main client's placeholder.
`account_inject.py` validates that placeholder against the exact Chrome bridge
host, account UUID path, and first `claude-code` connect frame before it creates
an aggregate session. Plain legacy placeholders and unrelated real logins keep
their previous account-local behavior.

`scripts/account_chrome_bridge.py` owns each client connection's account channels,
device routes, outstanding calls, and permission replies. It uses Tornado's
WebSocket client, already required by mitmproxy 12.2.3. There is no new listener,
credential refresh owner, or external dependency. The original upstream socket
anchors the main account. Secondary accounts have their own authenticated sockets.

Native discovery returns stable, native-valid UUID5 IDs. Display names come from
confirmed account-bound entries in the private `~/.claude/chrome-profiles.json`
identity map and use `host - GoogleEmail`. The private accounts file can override
that map's location with `chrome_profiles_file`. Unverified identities use
`Unidentified browser <id-prefix>`, never a generic account alias. Names remain
subject to the native client's 50-character limit. Only envelope device identities change. Native tool arguments, results,
notifications, file preparation, and permission handling remain with Claude Code.
Injected mitmproxy messages are excluded from the transformation hooks.

Actions require a known, connected device. Tool results and external-message
results must belong to the account that received the original request. Permission
requests get account-scoped request IDs, and permission responses must match the
original device and account. Failed accounts lose their device routes and owned
pending calls fail without account fallback. A disconnected in-flight request
reports its outcome as unknown, not as an assurance that no action executed.
Discovery failures emit a native
bridge error and set `chrome_account_statuses` on the flow. Failed accounts are
never represented as browser rows. A timed-out discovery channel is closed before
retry because the vendor discovery protocol has no request IDs.

`--chrome-account` is now honored by `--via`. If it selects another account, the
primary socket still authenticates its own original account UUID and token. The
session exposes and routes only the selected account's side channel. Local
`--chrome-account` behavior remains unchanged. Default local, non-`--via` native
aggregation is not part of this change.

The launcher's MCP snapshots remain separate. `aggregate_mcp()` copies placeholder
credentials for account-isolated children and assigns `+chrome=<alias>` to each
copy. The main client retains `+chromes`, Remote Control ownership, and privacy
suffixes. This prevents the existing `account-chrome` adapter from aggregating an
already-aggregated child or mislabelling another account's browsers.

## Checks

The pinned mitmproxy environment ran the native bridge, account injection,
launcher, explicit Chrome route, and Remote Control route suites. Result:
86 passed and two existing skips. One skip needs a built Rust binary in this
worktree. The other needs the optional `claude_swap` package in that test
environment. New coverage includes a real loopback Tornado WebSocket permission
roundtrip, account collision handling, injected-frame recursion prevention,
account and request ownership, timeout recovery, disconnect cleanup, legacy
client isolation, and launcher child snapshots.

The separate FastMCP 4.1.1 and artifact CLI 0.3.0 environment ran the existing
Chrome and artifact adapter suites with `pytest-asyncio`. Result: 23 passed and
one opt-in live-account test skipped. An initial run without `pytest-asyncio`
failed only because that test plugin was absent. It passed after the plugin was
included.

Ruff checks and formatting pass for the new bridge and its tests. Checking all
affected files also reports three pre-existing lint findings: the broad HTTP
exception boundary in `account_inject.py`, `re.I` in the launcher, and the
launcher's existing settings validation exception type. They were not changed.
`git diff --check` passes.

## Live coordinator verification

An isolated proxy on a separate port was tested with the installed Claude Code
2.1.310. Its reserved native Chrome MCP server returned four live browsers from
two accounts. The interactive native `/chrome` selector displayed the same four
connections. A later probe matched every returned `host - GoogleEmail` label to
the verified private identity map.

At the user's request, native selection and browser batches opened Google Account
in a separate tab in each of the four connected browsers. JavaScript and native
page reads verified in-tab notification banners. Browser OS-notification
permissions were not granted, so no OS notification delivery is claimed. Three
profiles showed authenticated Google Account pages. One showed its account email
on Google's re-verification page. No identity was switched and no password was
entered. These requested tabs remain open.

The initial blank-tab probe hit native API restrictions rather than an aggregation
failure: `about:blank` navigation is rejected, and tab creation returns text rather
than JSON. After closing the owned initial group tab, another owned blank tab was
no longer addressable by the native group tools. It was not force-closed through
another channel. The user approved a neutral HTTPS test, then requested Google
Account pages instead. A short native browser wait was required before page reads
because navigation completion can precede the document leaving the new-tab page.

Independent review confirmed one in-flight failure wording defect. The fix and
fault-injection regression passed independent re-review. The naming callback also
passed independent review. The coordinator's expanded routing suite passed 89
tests with the same two skips.

No commit, push, production proxy restart, or desktop change occurred. Only the
owned isolated test proxy was started or restarted. Production activation remains
a separate step.
