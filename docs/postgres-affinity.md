# Optional PostgreSQL account affinity (preparation only)

Off by default. `AccountInject.start` remains the account-selection owner. No credentials,
refresh grants, Remote Control records, usage counters or fallback outcomes enter this store.
This is not a standby deployment, complete proxy HA, or Remote Control replication.

## Opt in explicitly

1. Install the optional driver through the existing package: `uv pip install '.[affinity]'`
   in the environment that runs mitmproxy. No driver is imported in default mode.
2. Provision a dedicated application database and restricted role through the database owner.
   Use the existing Patroni service; this feature neither installs nor configures Patroni.
3. Bootstrap once with `scripts/account_affinity.sql` using that database's schema owner.
   For example, configure a private libpq service named `ccp-affinity`, then run
   `PGSERVICE=ccp-affinity psql -v ON_ERROR_STOP=1 -f scripts/account_affinity.sql`.
   The runtime role needs schema USAGE and SELECT, INSERT, UPDATE on `ccp_affinity`.
   Bootstrap uses the connection's schema/search path; every worker must use the same one.
   Runtime does not create tables. Missing table/driver/permissions fails closed.
4. Set both environment variables only for a separately approved standby process:

   ```sh
   export CCP_AFFINITY_DSN='service=ccp-affinity'
   export CCP_AFFINITY_NAMESPACE='standby-validation'
   ```

   A namespace is 1–128 characters, beginning with an alphanumeric character, then
   alphanumerics, dots, underscores, colons or hyphens. Partial/empty settings refuse startup.
   DSN accepts psycopg/libpq parameters, including multi-host connections with
   `target_session_attrs=read-write`. Require TLS on remote connections; verify the leader
   route from every intended node before any rollout. Keep passwords in a private
   `PGPASSFILE`/libpq service, not command arguments or checked-in config.

## Semantics and failure boundary

- All nodes in a logical proxy deployment use the same namespace, account aliases/order,
  disabled list and balance settings. Unrelated deployments use different namespaces.
- One row per namespace stores only session → [alias, last-seen epoch] plus round-robin cursor.
  INSERT-on-conflict and SELECT FOR UPDATE serialize first assignment, selection and commit.
  PostgreSQL time governs the existing one-hour idle expiry. Each transaction prunes idle rows
  and explicitly sets `synchronous_commit=on` locally, overriding loss-tolerant role/database
  defaults for these critical decisions without changing any cluster or persistent setting.
- The existing policies remain unchanged: session, prefer, pin, round-robin and least-sessions.
  Only the two spreading policies retain sticky session entries, as before. A disabled existing
  sticky owner stays sticky; removed aliases may be reassigned. Pin does not overwrite a prior
  balanced assignment. Counts and messages share selection. Credential/fallback policy is unchanged.
- Opted-in model POSTs using a placeholder or issued proxy API key enter one dedicated worker.
  There are at most 16 outstanding jobs per addon, including the running job; excess gets 503.
  The async hook never waits synchronously on PostgreSQL. Non-model control traffic and
  caller-owned tokens bypass that queue and retain their existing behavior.
- Missing/unavailable shared state, driver errors and commit errors return sanitized 503 without
  a credential pool. No local affinity fallback is allowed, even for pinned/deterministic policy.
  Existing explicit inference quota/auth fallback is independent and is not disabled.
  Connection timeout is 3 seconds per host; statement/lock timeout is 3 seconds per operation,
  not an end-to-end request deadline. Canceled clients do not free still-running worker slots.

## Limits and validation

The namespace row rewrites all active session state on every selection and serializes allocations.
This deliberately narrow implementation needs load validation before production use. It opens a
connection per model request; there is no pooling, automatic schema migration, session import,
config replication, refresh coordination, health-route change, failover orchestration or RC replication.
Do not mix local-store and shared-store workers for one deployment. Existing process-local affinity
is not migrated. Database durability/RPO remains that of the existing cluster: asynchronous
replication can lose acknowledged assignments on promotion. `synchronous_commit=on` currently
provides local durability, not synchronous replication or zero RPO. No cluster failover was run here.

Run `tests/account_inject_test.py` plus `tests/account_affinity_test.py` with pytest, mitmproxy,
claude-swap and the affinity extra installed. Integration tests opt in through `CCP_HA_TEST_DSN`
(and `PGPASSFILE` if needed), refuse any DB name except `ccp_ha_validation`, bootstrap the table,
use unique namespaces, and delete only their own rows. Without that variable, DB tests skip.
Use `--tb=short`; fixture connection failures suppress private details. Tests use toy credentials
and an in-memory mock upstream, never real inference/auth/refresh requests.
