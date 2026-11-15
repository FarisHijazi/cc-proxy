# Optional PostgreSQL shared logins (grants)

Off by default. With `CCP_GRANTS_DSN` unset, `account_inject.py` serves logins exactly as before:
cswap slots through `slot_token`, renewal through `renew`. With it set, every alias in the accounts
file is served from the `ccp_grants` table instead, so a primary and a standby proxy share ONE login
per account. Codex grants are not covered yet. Account affinity is a separate opt-in
(`docs/postgres-affinity.md`, plain path).

Invariant: in this mode an account's refresh token lives only in PostgreSQL (plus this host's
encrypted journal, below), and no host POSTs generation G to the provider without a committed lease
on G. OAuth refresh tokens rotate on every use, so two holders of one grant burn it.

## Opt in

1. Install the driver: `uv pip install '.[affinity]'` (the same `psycopg[binary]` extra).
   `cryptography` already comes with mitmproxy.
2. Bootstrap once as the schema owner:
   `PGSERVICE=ccp-grants psql -v ON_ERROR_STOP=1 -f scripts/account_grants.sql`.
   The runtime role needs SELECT, INSERT, UPDATE on `ccp_grants`. Runtime never creates tables.
3. Make a key, the same file on every host of the deployment:
   `umask 077; head -c 32 /dev/urandom | base64 > ~/.config/cc-proxy/grants.key`.
   The proxy refuses a key file readable by group or others.
4. Seed each account from the host whose cswap holds it, then follow the command it prints:

   ```sh
   scripts/account_grants_seed.py <slot> <alias>
   ```

   It encrypts the slot's login into the table and deletes nothing. It refuses the live login and an
   existing row (`--replace` stores a new generation and clears `needs-reconcile`).
5. Set all three variables for the proxy process (partial settings refuse startup):

   ```sh
   export CCP_GRANTS_DSN='service=ccp-grants'
   export CCP_GRANTS_NAMESPACE='prod'
   export CCP_GRANTS_KEY_FILE=~/.config/cc-proxy/grants.key
   ```

   The namespace follows the affinity rules. Keep the password in `PGPASSFILE`, never in the DSN.

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

## Tests

`tests/account_grants_test.py` with a fake provider and toy tokens: two hosts racing post once, a
crash before commit and a lost commit recover from the journal, `invalid_grant` never loops,
tampered ciphertext or wrong associated data fails closed, proxy 401 renewal posts once, the
unreachable store answers a sanitized 503, the live-login guard, and feature off keeps
`slot_token` and `renew`. DB tests use `CCP_HA_TEST_DSN` (database `ccp_ha_validation` only, a
unique namespace, its rows deleted) and skip without it.
