# Opt-in PostgreSQL grant authority for Claude logins, 2026-11-15

Branch `proxy-grants-1115` from `proxy-ha-1114`. No deployment, no service touched, no real
credential read, no auth endpoint contacted.

## Why

Two proxies (primary and standby) must share one login per account. Refresh tokens rotate, and
today each host guards refresh with a local lock only (cswap `consume_backup_grant`), so two hosts
holding the same grant would burn it.

## Changes

- `scripts/account_grants.py` (new): `PostgresGrants` with lease, POST, journal, CAS commit,
  reconcile and seed; AES-GCM sealing bound to namespace, account and generation.
- `scripts/account_grants.sql` (new): the `ccp_grants` table.
- `scripts/account_grants_seed.py` (new): moves one cswap slot into the table; deletes nothing,
  prints `cswap remove <slot>`. Not run against real credentials.
- `scripts/account_inject.py`: opt-in on `CCP_GRANTS_DSN` + `CCP_GRANTS_NAMESPACE` +
  `CCP_GRANTS_KEY_FILE`. `fetch` and `renew` take the grants path only when it is set; a 401 hands
  the rejected token to the next fetch as `stale`, throttled to once a minute. `GrantsUnavailable`
  answers the affinity-style sanitized 503. The live-login guard runs in `read_accounts`.
- `docs/postgres-grants.md`: setup, protocol and remaining failure cases.

## Deviation from the design pass

The journal is not deleted after the commit. It stays until the row holds a newer generation.
Deleting it at commit (as designed) leaves nothing to repair a commit that a later async failover
loses, which was one of the stated recovery goals.

## Validation

`uv run --no-project --with pytest --with 'psycopg[binary]>=3.2,<4' --with mitmproxy --with
'claude-swap @ git+https://github.com/FarisHijazi/claude-swap' python -m pytest
tests/account_inject_test.py tests/account_affinity_test.py tests/account_grants_test.py -q
--tb=short` against `ccp_ha_validation`: **80 passed** (67 existing + 13 new). Without
`CCP_HA_TEST_DSN` the grants file gives 6 passed, 7 skipped. Focused Ruff (`E4,E7,E9,F,I`) and
`git diff --check` pass.
