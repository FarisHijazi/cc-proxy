# Tailnet-only account proxy, any-machine `--via`, Chrome for every account

Follows `docs/devlog/claude_20261019-shared-account-proxy-serve-via.md`; that
file's key setup is superseded.

## Changes

- **No proxy key.** `--serve` no longer writes `proxyauth`; `--proxy-key-file` is
  gone. Safety is the bind address: `--listen-host` must be a tailnet IP.
  ftower: removed `ca/config.yaml`, `~/.config/claude-codex/proxy-key` and the
  flag from `claude-account-proxy.service`. Mac: removed `ftower-proxy-key`.
- **`--via` needs no local logins.** `placeholder_login()` calls
  `/api/oauth/profile` through the proxy with `Bearer proxy-account-<alias>` and
  writes the account metadata + a placeholder credential (claude.ai scopes,
  subscription type from `organization_type`, year-2100 expiry). Any machine on
  the tailnet only needs the accounts file (aliases) and the server's CA, and
  its `~/.claude/settings.json` is never touched (the launcher passes `--settings`).
- **`--via` aggregates every alias** via `aggregate_mcp`, like local mode.
- **Bug fix (both modes): main account's Chrome was never aggregated.** Claude on
  macOS moves the main login from `CLAUDE_SECURESTORAGE_CONFIG_DIR/.credentials.json`
  into the keychain at startup, so `account_chrome`'s main child saw no file
  ("account snapshot could not be read"). `aggregate_mcp` now gives main's Chrome
  child its own copy (`<login>/<main>-chrome-secure`).

## Verified

- curl through `100.103.162.14:18790` without credentials: `personal` and `work`
  profiles both answer.
- `claude-accounts -- -p ...` and `claude-accounts-local -- -p ...`:
  `list_connected_browsers` returns both accounts' browsers, both "available".
- Tests: router/inject/route suites 44 passed. `test_session_owned_router_relays_account_requests_and_exits`
  fails identically on the previous commit (curl exit 56), not caused by this.
  Run: `uv run --no-project --python 3.12 --with mitmproxy --with pytest --with ~/Projects/claude-swap pytest -q tests/account_router_test.py tests/account_inject_test.py tests/account_chrome_route_test.py tests/account_remote_route_test.py`
