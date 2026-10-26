# Email account aliases

Aliases like `work` and `personal` hid which login a session used: three clients
had `main: work` (the team login) while the proxy's `balance.prefer` pointed
inference at the other account, so artifacts, Remote Control and `/usage` went to
the wrong account.

- The alias pattern in `claude-account-router`, `account_inject.py` and
  `account_chrome.py` is now `[A-Za-z0-9][A-Za-z0-9._@-]{0,63}`, so emails fit.
  `+` stays a placeholder separator and `/` stays out of state paths.
- `account_inject` reads an optional `renamed` map (old alias to current alias)
  and applies it wherever a client names an account: inference, account
  endpoints, the Remote Control owner and the Chrome bridge. Sessions started
  before a rename keep their old placeholder bearer and keep working.
- Rollout order on the shared proxy: deploy the addon first (mitmproxy reloads
  scripts on change), prove it with a `renamed` probe that only the new code
  resolves, then rewrite the accounts file.

Verified: `tests/account_inject_test.py` covers email aliases, renamed old
aliases and a `renamed` target that does not exist. Full Python suite: 126 passed,
1 skipped (with `fastmcp`, `claude-artifact-cli`, `pytest-asyncio`, `mitmproxy`
and `../claude-swap`). Live on the shared proxy: old and new aliases both resolve
to the right login's profile.
