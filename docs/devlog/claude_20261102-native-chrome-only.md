# Built-in Chrome only, aggregated in the proxy

## Why

The launcher used to register a custom `account-chrome` MCP server in every session. That server ran one
`claude --claude-in-chrome-mcp` child per account and merged their lists. The newer launcher also hid the built-in
`claude-in-chrome` tools. On one client VM the custom server's children came up at boot and then answered
`Browser extension is not connected.` forever. Every account returned an empty list, so sessions saw no browsers
at all. The owner asked for the built-in Chrome tools only, with aggregation done in the proxy.

That aggregation already existed and was live. For `--via` clients, the main placeholder carries `+chromes`. Then
`account_inject.py` hands the native bridge WebSocket to `account_chrome_bridge.py`, which merges every account's
browsers into the native bridge (`claude_20261024-native-chrome-bridge-aggregation.md`).

## Change

`aggregate_mcp()` in `scripts/claude-account-router` now registers only `account-artifacts` and never passes
`--disallowedTools mcp__claude-in-chrome`. `scripts/account_chrome.py` and its test are deleted. `--chrome-account`
still routes the native bridge to one login. Without `--via`, native `/chrome` sees main's browsers only.

## Checks

The two router tests that pinned the old servers were changed first, and they failed on the old launcher. Afterwards all 54 router tests passed.
The Python suite, excluding the artifact test that needs its own FastMCP environment, gave 235 passed and 1 failed.
The failure was a bridge timeout test, and that file passed 15/15 on three reruns. The bridge was not changed.

On both client VMs, the launchers were patched in place. Backups are under
`~/.local/state/claude-codex/before-native-chrome-only-20261102/`. On each VM, a throwaway headless session
(`claude-accounts -- -p ...`, allowed only `mcp__claude-in-chrome__list_connected_browsers`) listed browsers from
two Claude accounts. The rows were labelled `<host> - <Google account>`. Running sessions keep the tools they started
with until they restart.
