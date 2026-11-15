# Rollback export for the PostgreSQL grant authority, 2026-11-15

Branch `proxy-grants-1115`. No deployment, no service touched, no real credential read, no auth
endpoint contacted.

## Why

After the first shared refresh the seed's source copy is dead, so a cutover had no way back short
of a new login. The export moves the current generation back to a local holder.

## Changes

- `scripts/account_grants.py`: state `exported`. `PostgresGrants.export` marks the row `exported`
  by CAS (state `ok` or `exported`, no live lease; polls up to `lease_wait_s`) and returns its
  credentials and generation. `current` answers `was exported` for it; `reconcile` never writes a
  journal back over an exported row. `write_private_json` (the atomic 0600 writer, now shared by
  `write_codex_auth`) and `store_from_env` (the `CCP_GRANTS_*` reader, now shared by the seed).
- `scripts/account_grants.sql`: idempotent widening of the `state` check. Existing deployments
  rerun it as the schema owner.
- `scripts/account_grants_export.py` (new): `<alias> <slot>` writes through cswap's public
  `persist_backup_credentials` (locked, backup store only); `--codex <name> <path>` writes a full
  `auth.json`, refusing an existing file without `--force`. Destination checks run before the row
  is touched.
- `docs/postgres-grants.md`: "Rolling back", two new remaining failure cases, and a note to keep a
  `cswap export` of the slot before `cswap remove`.

## cswap write path

cswap has no public API that creates a slot from bare credentials: `import_accounts` needs the
`oauthAccount` identity, which the table does not hold. So the export writes only into an existing
slot, and a removed slot is restored first with `cswap import` of an export kept at seed time.

## Validation

`tests/account_grants_test.py` gains 4 DB tests (cswap rooted in a temp `HOME`/`XDG_DATA_HOME`/
`CLAUDE_CONFIG_DIR`, slot created through `cswap import`). Mutation check: removing the reconcile
guard and the re-export allowance fails 2 of them. The full four-file suite against
`ccp_ha_validation`: **101 passed** (97 before + 4). Focused Ruff (`E4,E7,E9,F,I`) passes.
