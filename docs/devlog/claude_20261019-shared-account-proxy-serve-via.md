# One machine holds the logins: `--serve` / `--via`

Goal: run the account proxy on one tailnet box and keep every Claude login
there; laptops connect with a fixed key and hold no usable credential.

## Shape

- `claude-account-router --serve` (server): the existing `proxy_command`
  (mitmproxy, all endpoints mapped to a loopback `claude-codex`) bound to
  `--listen-host`, with `proxyauth` from the confdir's `config.yaml` (the key
  never reaches argv) and `scripts/account_inject.py`. The bridge host is
  intercepted too.
- `account_inject.py`: `Authorization: Bearer proxy-account-<alias>` and the
  Chrome bridge's `connect` frame `oauth_token` are swapped for that alias's
  token from the server's cswap. The live default login is only read (claude
  refreshes it); other slots refresh through `consume_backup_grant`. Tokens are
  cached until 5 min before expiry. Real tokens pass through; an unknown alias is
  answered 401 locally, never forwarded.
- `claude-account-router --via http://HOST:PORT` (client): no local proxy.
  `bootstrap_cswap(placeholder=...)` writes the slot's metadata and scopes with
  accessToken `proxy-account-<alias>` and a year-2100 expiry, so the client never
  refreshes. Main account only: no secondaries, `--chrome-account` or HQ route yet.

## Deployment (ftower, 2026-10-19)

- Router: this fork built in `rust:1-bookworm` (the box has no C toolchain), as
  `~/.local/bin/claude-codex-fork`; the older `claude-codex.service` on the docker
  bridge is untouched.
- `uv tool install mitmproxy --with <claude-swap fork>` (the addon imports cswap).
- `claude-account-proxy.service` (systemd user, `Restart=always`):
  `100.103.162.14:18790`, map `~/.config/claude-codex/accounts.json`, key
  `~/.config/claude-codex/proxy-key` (0600). Source is shipped with
  `git archive HEAD | ssh ftower tar -x -C ~/src/claude-code-with-codex`.
- Mac: key and the server CA are in `~/.config/claude-codex/ftower-{proxy-key,ca.pem}`;
  `~/.local/bin/claude-accounts` uses `--via`, the old all-local launcher is
  `claude-accounts-local`.
- Slot 1 was copied from the Mac with `cswap export --account 1` / `cswap
  import --force` (user's choice). The rotating grant is now shared, so only ftower
  may refresh it: on the Mac use it only through `--via`.

## Verified

No key → `407`; bogus alias → local 401; `personal` profile → the right account;
`claude -p` through `claude-accounts` answered with only the placeholder on the Mac.
Tests: `tests/account_inject_test.py` (4) with the mitmproxy suites, which need
Python 3.12: `uv run --no-project --python 3.12 --with mitmproxy --with pytest pytest -q
tests/account_inject_test.py tests/account_chrome_route_test.py` → 17 passed.

## Open

`work` on ftower: its `~/.claude.json` names the work account but it holds no
credential, so nothing to inject. Copying the Mac's work grant would share the
Mac's live default login's rotating token (one side logs out); a fresh login on
ftower + `cswap add` avoids that.
