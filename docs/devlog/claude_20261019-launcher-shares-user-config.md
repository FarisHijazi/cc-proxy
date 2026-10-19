# Launcher sessions now share the user's Claude config

Problem: a `claude-accounts` session showed different `/config` values, no
plugins/skills and no hooks. The launcher's `CLAUDE_CONFIG_DIR` was a blank
profile (only `oauthAccount`/`hasCompletedOnboarding`/`theme`), and its CLI
settings forced `disableAllHooks`. That isolation was a choice in
[the router devlog](claude_20261018-account-inference-router.md), now reversed.

Change (`scripts/claude-account-router`):

- `share_user_config` calls cswap's own share-all mirroring
  (`SessionManager._sync_sharing(..., share_all=True)`, what `cswap env`
  profiles use) on the main client config dir. Everything in `~/.claude` is
  symlinked except credentials, `.claude.json`, `sessions/`, `ide/`,
  `statsig/` and `backups/` (cswap's `NEVER_SHARED_NAMES`). History
  (`projects/`, `history.jsonl`) is shared too; an existing per-profile copy is
  merged into `~/.claude` by cswap once no session uses the profile. cswap
  never replaces a real entry, so the launcher first moves any pre-share copy
  Claude created in its own profile (e.g. a `settings.json` holding only
  `remoteControlAtStartup`) to `pre-share-<epoch>/` beside the profile.
- `update_account_metadata` seeds `.claude.json` from `~/.claude.json` every
  launch, except `ACCOUNT_SCOPED` keys (identity, `cached*`/`*Cache` feature-flag,
  plan and model-access caches), which stay the profile's own — the
  computer-use flag is read from `cachedGrowthBookFeatures` and is per account.
- `disableAllHooks` is no longer set. The launcher's `--settings` still wins
  over a shared `settings.json` for the base URL, proxy and credential env.

Limit: `/config` changes that Claude stores in `.claude.json` (not
`settings.json`) made inside a launcher session are overwritten by
`~/.claude.json` on the next launch; change them in a normal session.

Checks: account router/chrome/artifacts suites 41 passed, 1 skipped (the live
mitmproxy relay test deselected, failing before this change). A scratch profile
got all `~/.claude` entries linked, a real `.claude.json` with 109/139 keys
mirrored, and the account identity kept.
