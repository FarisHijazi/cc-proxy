# Codex login in the shared grant table, 2026-11-15

Branch `proxy-grants-1115`, on top of the Claude grant authority
(`docs/devlog/claude_20261115-postgres-grants.md`). No deployment, no service touched, no real
credential read, no auth endpoint contacted.

## Why

A primary and a standby proxy must share one Codex login. The router refreshed
`$CODEX_HOME/auth.json` itself, under a tokio mutex that covers one process, and wrote it by
rename without a sync. Two hosts holding that file would spend the rotating refresh token twice.

## Changes

- `src/providers/codex/auth/manager.rs`: `CCP_CODEX_EXTERNAL_REFRESH=1` (off by default) makes
  `get_auth` and `force_refresh` use only the file they just re-read. No POST. A token is used
  until its real expiry; a missing, expired or just-rejected token is an error naming the file
  and the variable.
- `scripts/account_grants.py`: `PostgresGrants` takes a `parse` (credential shape) next to its
  `refresh`. `current()` returns the decrypted credentials; `token()` parses them. The protocol is
  unchanged and still one implementation. New: `codex_login`, `codex_refresh` (the router's form
  POST, injectable `post`), `write_codex_auth` (access-only, 0600, fsync, rename, directory fsync),
  and `CodexKeeper` (60 s loop).
- `scripts/account_inject.py`: `CCP_GRANTS_CODEX_ACCOUNT` + `CCP_CODEX_AUTH_FILE` start the keeper.
  It refuses without the `CCP_GRANTS_*` settings, an invalid name, or the Codex CLI's own file.
- `scripts/claude-account-router`: `--serve` adds `CCP_CODEX_EXTERNAL_REFRESH=1` to the router's
  environment only when `CCP_GRANTS_CODEX_ACCOUNT` is set, and refuses it without
  `CCP_CODEX_AUTH_FILE`.
- `scripts/account_grants_seed.py`: `--codex <auth.json> <name>` seeds row `codex:<name>`.
  Smoke-tested against the validation database with a toy file (seeded generation 1, a second
  seed refused, rows deleted).
- `scripts/account_codex_quota.py`: reads `CCP_CODEX_AUTH_FILE` first, matching the router.

## Deviations from the design pass

- In external mode a token near expiry is still served until it really expires. Rejecting it at
  the router's 5-minute margin would race the keeper, which refreshes inside that same margin.
- Naming: the row is `codex:<name>` with `<name>` chosen by the operator, not a fixed `codex`, so
  one table can hold several Codex logins later. A Claude alias cannot contain `:`.

## Remaining risks

- The router never tells the keeper about a 401, so a Codex token revoked early stays broken
  until the keeper's next refresh near expiry or a reseed.
- Only `--serve` wires the router variable. A router started any other way with a keeper-written
  file falls back to its own refresh, finds no refresh token, and fails as it did before.
