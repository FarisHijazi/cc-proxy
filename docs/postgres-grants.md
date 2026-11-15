# Optional PostgreSQL shared logins (grants)

Off by default. With `CCP_GRANTS_DSN` unset, `account_inject.py` serves logins exactly as before:
cswap slots through `slot_token`, renewal through `renew`. With it set, every alias in the accounts
file is served from the `ccp_grants` table instead, so a primary and a standby proxy share ONE login
per account. The Codex CLI login can join the same table (below). Account affinity is a separate
opt-in (`docs/postgres-affinity.md`, plain path).

Invariant: in this mode an account's refresh token lives only in PostgreSQL (plus this host's
encrypted journal, below), and no host POSTs generation G to the provider without a committed lease
on G. OAuth refresh tokens rotate on every use, so two holders of one grant burn it.

## Opt in

1. Install the driver: `uv pip install '.[affinity]'` (the same `psycopg[binary]` extra).
   `cryptography` already comes with mitmproxy.
2. Bootstrap once as the schema owner:
   `PGSERVICE=ccp-grants psql -v ON_ERROR_STOP=1 -f scripts/account_grants.sql`.
   The runtime role needs SELECT, INSERT, UPDATE on `ccp_grants`. Runtime never creates tables.
   The file is idempotent; rerun it after an upgrade (it widens the `state` check to `exported`).
3. Make a key, the same file on every host of the deployment:
   `umask 077; head -c 32 /dev/urandom | base64 > ~/.config/cc-proxy/grants.key`.
   The proxy refuses a key file readable by group or others.
4. Seed each account from the host whose cswap holds it, then follow the command it prints:

   ```sh
   scripts/account_grants_seed.py <slot> <alias>
   ```

   It encrypts the slot's login into the table and deletes nothing. It refuses the live login and an
   existing row (`--replace` stores a new generation and clears `needs-reconcile` or `exported`).
   To keep a way back without a new login, run `cswap export <file> --account <slot>` (0600) before
   `cswap remove`: it keeps the slot's identity. Its token goes dead at the first shared refresh;
   until then the file is a second holder, so nothing may use it but `cswap import`.
5. Set all three variables for the proxy process (partial settings refuse startup):

   ```sh
   export CCP_GRANTS_DSN='service=ccp-grants'
   export CCP_GRANTS_NAMESPACE='prod'
   export CCP_GRANTS_KEY_FILE=~/.config/cc-proxy/grants.key
   ```

   The namespace follows the affinity rules. Keep the password in `PGPASSFILE`, never in the DSN.

Setup-tokens (`claude setup-token`: no refresh grant, no expiry) never rotate, so any number of hosts
may hold a copy. In this mode an alias whose cswap slot holds one stays on cswap (`AccountInject.copyable`)
and is never seeded or renewed; only rotating logins come from the table. A host's live login can be
seeded with `scripts/account_grants_seed.py --file ~/.claude/.credentials.json <alias>`; then move that
file aside and drop `oauthAccount` from `~/.claude.json` before the proxy restarts.

The proxy refuses to start when an alias is this host's live default Claude login (cswap's current
slot, or the `~/.claude.json` email). Claude Code refreshes that login itself, outside any lease.

## How a login is served

- Read: `PostgresGrants.token` decrypts the row and returns its access token and expiry.
  `AccountInject.cache` keeps it until `MARGIN_MS` before expiry, as before.
- Refresh, when the token is within `MARGIN_MS` of expiry or Anthropic rejected it (401):
  1. Lease: `UPDATE ... WHERE generation = G AND state = 'ok' AND lease expired`, committed with
     `SET LOCAL synchronous_commit = on`. Lease length is 120 s; the POST times out at 10 s.
  2. POST through `claude_swap.oauth.try_refresh_oauth_credentials`, with no transaction open.
  3. Write `<key dir>/grants-journal/<hash>.json` (0600, fsynced, then renamed and the directory
     fsynced) holding G+1 encrypted.
  4. Commit `generation = G+1` with compare-and-swap on `generation = G`.
- A host that loses the lease race polls the row every 0.5 s for up to 15 s, then answers 503.
- The journal is kept after the commit, until the row holds a newer generation. Every read first
  reconciles it: a journal newer than the row is written back. The proxy also reconciles all
  journals on start.
- `invalid_grant` or a login with no refresh token sets `state = 'needs-reconcile'`. Nothing POSTs
  that account again until a journal write-back or a reseed. A transient failure releases the lease.
- Every failure answers that account with a sanitized 503 (`api_error`). Messages name the alias,
  never a DSN, server detail or token. In a fallback pool the account is skipped, as any broken slot.
- Encryption is AES-256-GCM with a random nonce. Associated data is namespace, account and
  generation, so a row copied to another account or generation fails closed.

## The Codex login (opt-in on top)

The router's Codex backend normally refreshes `$CODEX_HOME/auth.json` itself, under a lock that
covers one process only. To share one Codex login across hosts, set two more variables for the
`--serve` launcher (both processes inherit them):

```sh
export CCP_GRANTS_CODEX_ACCOUNT=work            # row "codex:work"
export CCP_CODEX_AUTH_FILE=~/.local/state/cc-proxy/codex-auth.json
```

- The row is `codex:<name>`. `<name>` is `[A-Za-z0-9][A-Za-z0-9._@-]{0,63}`; Claude aliases match
  that same pattern, so no alias contains the `:` and the two kinds never collide.
- Seed it once, from a host's Codex CLI file:
  `scripts/account_grants_seed.py --codex ~/.codex/auth.json work`. Then remove that file as the
  seed prints, so the CLI no longer holds the grant. Do not use `codex logout`; whether it revokes
  the grant upstream is unverified.
- `account_inject.py` starts one `CodexKeeper` thread. Every 60 s it reads the row through the
  same `PostgresGrants` protocol (lease, POST, journal, CAS). Only the credential parser
  (`codex_login`: the access token's JWT `exp`) and the refresh function (`codex_refresh`) differ.
  The refresh mirrors the router: form POST to `https://auth.openai.com/oauth/token` with
  `client_id=app_EMoamEEZ73f0CkXaXp7hrann`, `grant_type=refresh_token`. A 401, OAuth
  `invalid_grant` or a `refresh_token_expired`/`_reused`/`_invalidated` code marks
  `needs-reconcile`.
- After each new generation, and whenever the file is missing, the keeper writes
  `CCP_CODEX_AUTH_FILE` atomically (temp file, fsync, rename, directory fsync, mode 0600) with the
  current access token and **no refresh token**.
- `claude-account-router --serve` then starts the router with `CCP_CODEX_EXTERNAL_REFRESH=1`. In
  that mode the router never POSTs a refresh. It re-reads the file on every request, uses a token
  until its real expiry (the 5-minute margin no longer triggers anything), and answers a clear
  error naming the file when the token is missing, expired, or the one Codex just rejected.
- The proxy refuses to start when `CCP_CODEX_AUTH_FILE` is the Codex CLI's own file
  (`$CODEX_HOME/auth.json` or `~/.codex/auth.json`), or when the `CCP_GRANTS_*` settings are absent.
  The dashboard's Codex quota card reads `CCP_CODEX_AUTH_FILE` first, as the router does.

With both variables unset, the router and the CLI file behave exactly as before.

## Rolling back

`scripts/account_grants_export.py` is the reverse of the seed. It marks the row `exported` (only
once no lease is live, waiting up to 15 s for a refresh in flight), then writes that generation
where a non-shared proxy reads it, and prints only the account, generation and destination.

```sh
cswap import <file>                                   # only if the slot was removed
scripts/account_grants_export.py <alias> <slot>       # cswap's own locked backup write
scripts/account_grants_export.py --codex work ~/.codex/auth.json   # full file, refresh token included
```

- An `exported` row answers the sanitized 503 (`was exported`) and is never leased or POSTed; a
  journal does not write it back. Then turn shared mode off for the account.
- The cswap slot must exist and must not be the live login. The Codex file is written atomically at
  mode 0600 and never over an existing file without `--force`.
- If the write fails, the row stays out of service; run the export again (an exported row can be
  exported again).
- `account_grants_seed.py <slot> <alias> --replace` puts it back in service as a new generation.

## Failure cases that remain

- Async failover can lose a committed G+1. Only the refreshing host's journal can write it back.
  If that host's disk is lost too, the account needs a new login. Until it reconciles, another host
  may POST G, get `invalid_grant`, and mark `needs-reconcile`. If the provider revokes the whole token
  family on reuse (not verified for Anthropic), the journal's G+1 is dead as well.
- A crash between the POST and the commit leaves G leased for 120 s and G+1 only in the journal.
  Other hosts answer 503 meanwhile, then POST the spent G and mark `needs-reconcile`. The crashed
  host repairs it on restart or its next read.
- Lease expiry before commit: if a POST outlives the 120 s lease, a second host can POST G. The
  first host still commits G+1 (the CAS checks only G).
- A transient POST error after the provider consumed G (lost response) leaves no G+1 anywhere.
  The next refresh gets `invalid_grant`.
- Any other holder of the same grant burns it. Remove the cswap slot (the seed prints the command),
  stop the cswap usage collector and auto-switcher for that account, and log out any CLI copy at
  cutover. Removing the slot also drops the sequence entry the Chrome bridge reads for the
  account's UUID.
- The live-login guard runs on start and on accounts file reload, not when someone runs `/login` on
  the host later.
- One connection per statement group, no pooling; a refresh opens about four connections.
- Export takes the row's generation. If another host crashed after POSTing it (G+1 only in that
  host's journal), the exported login is already spent, and that journal no longer writes back.
- A refresh POST that outlives its 120 s lease can still commit G+1 after an export of G.
- Codex: the keeper learns nothing from the router. A Codex access token revoked before its expiry
  keeps failing (with the router's clear error) until the keeper's next refresh near expiry, or a
  reseed. The router does not wait for a rewrite; it answers at once.
- Codex: if the keeper stops, the router serves the last written token until it expires, then
  errors. The access token in `CCP_CODEX_AUTH_FILE` is plaintext at rest (mode 0600), as the
  Codex CLI's own file is.

## Tests

`tests/account_grants_test.py` with a fake provider and toy tokens: two hosts racing post once, a
crash before commit and a lost commit recover from the journal, `invalid_grant` never loops,
tampered ciphertext or wrong associated data fails closed, proxy 401 renewal posts once, the
unreachable store answers a sanitized 503, the live-login guard, and feature off keeps
`slot_token` and `renew`. The Codex half runs the same file with a fake token endpoint: two
keepers racing post once and write access-only 0600 files, a crash after the POST recovers from
the journal, `invalid_grant` never loops, the refresh's request and error mapping, the opt-in
guards, and `--serve` setting `CCP_CODEX_EXTERNAL_REFRESH` only when opted in. Rollback: export
into a temp cswap slot then reseed round-trips the login, an exported row is never served, POSTed
or revived by a journal, export waits for a refresh in flight and refuses a lease that stays, and
the Codex export keeps the refresh token at 0600 and never overwrites without `--force`. Rust unit tests
in `src/providers/codex/auth/manager.rs` (`external_refresh_*`) pin the router side on a real
temp `auth.json` against a listener that must never be dialed. DB tests use `CCP_HA_TEST_DSN` (database `ccp_ha_validation` only, a
unique namespace, its rows deleted) and skip without it.
