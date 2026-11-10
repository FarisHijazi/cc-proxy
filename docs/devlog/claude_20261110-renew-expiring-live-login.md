# Renew the live login before it expires

At about 08:13 on 2026-11-10, the main login on the shared proxy (ftower, cswap slot 1, `~/.claude/.credentials.json`) expired, and every Remote Control call returned 401 for over an hour. Nothing on ftower renews that login: no interactive claude runs there, and the proxy only reads it. Yesterday's `renew_rejected` covered only an unexpired token that Anthropic rejected. The manual recovery was `env -u HTTPS_PROXY -u HTTP_PROXY -u ANTHROPIC_BASE_URL ~/.local/bin/claude -p "reply ok"`: Claude Code refreshed its own credentials under its own locks.

## Why cswap did not refresh it

The installed claude-swap 0.28.0b1 was measured on ftower. Token values were never printed.

- The slot backup is stale. It expired about 44 hours before the live token, because Claude Code rotated the live login and nothing resynced the backup. The live credential is therefore not attributable to the slot: its fingerprint differs from the backup's.
- With an expired live token, `_fetch_active_usage` reaches the "live newer than backup, ownership unverified" branch. It logs "refresh deferred to Claude Code's next use" and returns the `token expired` sentinel. This is by design: the provenance rule (#117) never spends an unattributed grant.
- `renew_rejected` also called `_fetch_active_usage` without `org_uuid`. Under the lock, `_live_identity_matches(email, "")` is then false whenever the account has an organization, so that path deferred every time, even before reaching the branch above. Yesterday's fix could never have refreshed the live login.

So cswap cannot safely do this, and it says itself that Claude Code should.

## How early a refresh can happen

Claude Code 2.1.318 refreshes only when `now + 300000 >= expiresAt` (function `Ex` in the binary; cswap's `OAUTH_EXPIRY_BUFFER_MS` mirrors it). A `claude -p` run 30 minutes before expiry does nothing. The earliest useful moment is 5 minutes before expiry, the same as the proxy's existing `MARGIN_MS`. The requested 30-minute lead is therefore not possible with Claude Code doing the refresh.

## Change (`scripts/account_inject.py`)

- `renew_rejected` became `renew(slot, stale)`. For the live slot it runs `renew_live_login()` when the live token is the rejected one, or when it has an `expiresAt` within `MARGIN_MS` of now (expired included). For a backup slot it still calls `consume_backup_grant`, but only for a rejected token. Backup expiry was already handled inside `slot_token`.
- `renew_live_login()` runs `claude -p "reply ok" --model haiku --settings '{"disableAllHooks": true}'` from `$HOME`, with `PROXY_ENV` removed, stdin closed and a 180 s timeout. Hooks are disabled so ftower's notification hooks do not fire on each renewal.
- `AccountInject.token()` calls `self.renew(alias, None)` whenever the token it is about to serve is within the margin or `slot_token` raised. `rejected()` calls the same `renew`. The thread and the once-per-minute-per-account rate limit are shared. While the live token is still valid it keeps being served, so a renewal has up to five attempts before expiry and keeps retrying after it.
- Fixed a related bug: `renew_in_background` logged failures with `ctx.log.warn`, which does not exist in mitmproxy 12.2.3 (ftower). Every failed renewal crashed its thread without a log line. It now uses `logging.warning`.

## Verification

- `tests/account_inject_test.py`: 49 passed. New tests cover: `renew` acts only on a rejected, expiring or expired live token, never on a far-from-expiry one or a setup-token; an expiring token is still served and renews once a minute, and an expired one renews too; `renew_live_login` strips the proxy env. An autouse fixture stops every other test from starting the real cswap or claude. The full Python suite, without `account_artifacts_test.py`, passed 262 tests. That file fails at collection for reasons unrelated to this change.
- On ftower, the exact command under the unit's PATH with an empty env returned `ok`, exit 0.
- Deployed at 14:44:19Z. The previous file is in `~/.local/state/claude-codex/before-expiry-renew-20261110/account_inject.py`. After the restart the unit was active. In the next two minutes, every non-inference flow injected for the main login returned 200 (17 flows plus 7 under other placeholders). The only 401s came from the `dev` client's own credential on `/api/oauth/usage`, which the proxy does not inject.
- Not reproduced live: an actual expiry. The live token had about 7.9 h left at deploy time. The next expiry exercises the path. Check `journalctl --user -u claude-account-proxy` for "renewing cswap slot" and the traffic DB for 401s around it.
