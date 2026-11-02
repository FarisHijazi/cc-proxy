# Tunnel clients failed DNS on a host with a refusing resolver

## Symptom

New Claude Code connections from one client VM through `wss://claude-proxy.<domain>` intermittently hung. The session's `tunnel.log` showed `failed to get a connection to the server from the pool: TimedOut` on about 12% of new connections, steadily for days. The wstunnel server never saw most of the client's attempts. Plain curl from the same host to the same URL succeeded in about 0.6 seconds.

## Cause

wstunnel resolves names with its own hickory resolver unless told otherwise. It reads every `nameserver` in `/etc/resolv.conf` and queries them in parallel. On that host, one listed resolver answers `REFUSED` in under a millisecond. The working resolvers take 100 to 350 milliseconds, so the refusal usually wins and wstunnel treats the lookup as failed. glibc queries servers in order and reaches a working one first, which is why other programs were unaffected.

## Fix

`reach()` in `scripts/claude-account-router` now starts the tunnel with `--dns-resolver system://0.0.0.0`, the libc resolver wstunnel documents. The tunnel then resolves exactly like every other program on the host. `tests/account_router_test.py::test_reach_tunnel_resolves_with_the_system_resolver` failed before the change and passes after.

On the affected VM, an isolated client with the system resolver answered 15 of 15 real proxied requests. The default resolver answered 11 of 15. The installed launcher's own `reach()` then answered 20 of 20, with no pool timeouts and a 0.75 second median.

Only newly launched sessions get the fix. Running sessions keep their existing tunnel process until they are relaunched. The refusing resolver in the host's DNS configuration was left unchanged.
