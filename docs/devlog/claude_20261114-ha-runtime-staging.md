# Proxy HA runtime staging, 2026-11-14

Preparation only. No production cutover, proxy restart, Docker restart, host reboot, or database failover was performed.

## Verified infrastructure

The running proxy remains on ftower VM 102 on pve3. The public wstunnel fallback and HQ backend share that host. Clients, including the session doing this work, point at ftower's tailnet address on port 18790. Do not stop that service to test HA.

The candidate standby is dev VM 101 on pve2. Its direct route to the Riyadh database subnet currently goes through `192.168.0.1`; connections to pg-1 and pg-2 timed out. Direct database connectivity remains a prerequisite for deployment. Temporary loopback SSH tunnels were used only for isolated tests, not as the proposed HA route. The final path went through pg-3 to pg-2 and passed all 67 combined tests. All test tunnels were closed afterward; port 15439 is no longer listening.

Patroni currently reports pg-2 as leader, timeline 6, and pg-1 and pg-3 streaming at zero reported lag. `synchronous_standby_names` is empty and both replicas are asynchronous. Existing automatic failover is not a zero-data-loss guarantee. No replication settings changed.

## Staged runtime checks

Copied the deployed binary and current Python runtime scripts, without credentials, to `~/.local/share/cc-proxy-standby/releases/20261114-ftower/`. Binary SHA-256 matches ftower:

`4f13dc0df3254bd7d7b8878c076c6a9bc141a723010fe77cd67de3e1abe85bd4`

The copied binary reports version 0.4.0. A loopback-only test used an isolated HOME, a nonexistent test account slot, and provider URLs pointed at an unused loopback port. CONNECT/TLS and the routed `/healthz` returned 200. Test launcher cleanup closed its listener. This proves startup and transport, not valid authentication or inference.

Debian HAProxy 3.0.11-1+deb13u4 and its missing tracing libraries were extracted under `~/.local/share/cc-proxy-standby/tools/`; no system package or service was installed. A dummy TCP-backend test proved new connections select primary, then backup after the primary listener closes, then primary after recovery. An existing connection remained on its original backend. It was neither migrated nor replayed. Real server loss still interrupts streams.

Tests used upstream HAProxy TCP `server ... check` and `backup`, with `retries 0`. References:

- https://www.haproxy.com/documentation/haproxy-configuration-tutorials/protocol-support/tcp/
- https://www.haproxy.com/documentation/haproxy-configuration-tutorials/reliability/health-checks/

## Isolated database

Created `ccp_ha_validation` with a dedicated non-superuser, non-createdb, non-createrole owner on Patroni. It is not a production proxy database and shares no application tables. Connection settings live in `~/.config/cc-proxy-standby/validation.env`; the password is in a separate mode-0600 pgpass file.

The first failed connection test exposed the initial validation password in a pytest traceback. That password was rotated before further tests. DSNs now omit passwords, and fixture errors are sanitized. No production credential was exposed.

The test tunnel also needed isolated SSH configuration. The inherited ftower SSH configuration requests reverse forwards on ports 8374–8376, which conflict when opening a separate connection with `ExitOnForwardFailure=yes`. The final test tunnel uses `-F /dev/null`, the existing dev identity, strict host-key checking, and only its explicit loopback forward. Twelve database connection probes succeeded through it. The shared SSH master was not stopped.

The opt-in affinity implementation and database test results are recorded separately by the implementation session in this worktree. The copied production runtime above has not been modified to enable it.

## Remaining deployment gates

- Direct, redundant connectivity from the standby to the database leader.
- Independent login grants per proxy, or a proven credential authority with effective fencing. A PostgreSQL advisory lock alone cannot fence an upstream refresh already in flight.
- Shared configuration and Remote Control ownership, plus availability of HQ itself.
- A client ingress deployment that can choose a standby without depending on ftower.
- Explicit approval before live traffic cutover. No claim of full proxy HA until end-to-end authentication, account affinity, failure recovery, and rollback are verified.

The harness-wrapping session was offline. Coordination messages were queued, not acknowledged.

## 2026-11-15: direct database path from the standby

`dev` now routes `192.168.50.0/24` through pve2 (ansible-proxmox `guest-peer-routes.yml`, group `standby_guests`), so the
SSH tunnel is gone. `~/.config/cc-proxy-standby/validation.env` holds a multi-host DSN (pg-1, pg-2, pg-3, port 5432,
`target_session_attrs=read-write`); the pgpass entry matches any host. The suite passes over it: 67 passed. The live proxy
on ftower was not touched (same PID, 0 restarts).

Still missing before a cutover: independent account logins on `dev` (Faris signs in), shared Remote Control / HQ state,
client ingress that can fail over, and Faris's approval.
