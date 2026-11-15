# Optional PostgreSQL affinity preparation, 2026-11-14

Work is isolated to branch/worktree `proxy-ha-1114`. The source checkout's tracked dirty
changes were captured with `git diff HEAD --binary` and applied before implementation.
No commits, production proxy changes, service restarts, real inference/auth/refresh requests,
Patroni changes or cluster failovers were performed by this implementation.

## Changes

- `scripts/account_inject.py`: explicit `CCP_AFFINITY_DSN` + namespace opt-in;
  `AccountInject.start` retains selection ownership. A row-locked namespace transaction stores
  only sticky session aliases/timestamps and round-robin cursor. Existing policy handles pin,
  prefer, session, spreading, disabled accounts and expiry. PostgreSQL time governs expiry.
- Critical decisions explicitly use transaction-local `synchronous_commit=on`; no persistent
  database/role/cluster defaults are changed. Current asynchronous replication still has RPO.
- Injected model POSTs use one worker behind an async hook and at most 16 outstanding jobs.
  Non-model control traffic bypasses that queue. Unavailable state/commit errors/full queue
  fail closed with sanitized 503, never process-local reassignment. Cancellation does not
  release a still-running worker slot. Existing credential refresh/fallback logic is unchanged.
- `pyproject.toml`: optional `affinity` extra using supported `psycopg[binary]` dependency.
- Explicit bootstrap: `scripts/account_affinity.sql`; opt-in, privileges, schema, DSN and
  rollout limits: `docs/postgres-affinity.md`.
- `tests/account_affinity_test.py`: toy credentials, in-memory mock upstream, atomic two-worker
  first assignment, shared least-session counts, restart continuity, expiry, policies,
  namespace isolation, disconnection/recovery, commit failure, bounded/canceled backlog,
  control-plane responsiveness and actual transaction durability setting.

## Validation

- Final combined regression: **67 passed**, including all 11 real PostgreSQL integration cases,
  run by the coordinator on dev against `ccp_ha_validation`. Command: `uv run --no-project
  --with pytest --with 'psycopg[binary]>=3.2,<4' --with mitmproxy --with
  'claude-swap @ git+https://github.com/FarisHijazi/claude-swap' python -m pytest
  tests/account_inject_test.py tests/account_affinity_test.py -q --tb=short`.
- Offline regression: **56 passed, 11 DB tests skipped** without a validation DSN.
- Earlier ftower test-tunnel attempts timed out intermittently. The final run used a dedicated
  loopback SSH forward through pg-3 to pg-2, independent of ftower. Twelve connection probes
  passed before the full suite. No live service or network route was changed.
- Focused Ruff (`E4,E7,E9,F,I`) and `git diff --check` pass. Broader Ruff still reports three
  inherited diagnostics in unchanged credential-renew/model-listing/error-handling paths
  (`LOG015`, `S110`, `BLE001`); those unrelated user edits were preserved.
- The initial validation password accidentally appeared in a third-party pytest connection
  traceback; the coordinator rotated it, moved it to a private pgpass file, and supplied
  password-free DSNs. Fixture connection failures now suppress private connection details.

## Remaining boundary

All 11 integration cases passed against the isolated validation database. The temporary tunnel
is NOT an HA database route. Direct standby-to-leader connectivity and production
load/rolling-transition validation remain deployment gates.
One namespace JSONB row rewrites all active state and serializes allocations; this is a
narrow preparation slice, not complete proxy HA or Remote Control/credential replication.
