# Cascading `--serve` proxies

`--serve --via http://NEXT:PORT --via-ca NEXT-CA.pem` makes one account proxy use
another as its backend.

## What changed

- `scripts/account_inject.py`: new `proxy_upstream` option. `cascades()` is true when
  a placeholder names an alias this hop does not hold (the `rc=` owner on Remote Control
  paths). Such requests keep the placeholder and get no pool header, so the next hop
  swaps it and runs its own fallback. Malformed placeholders are still refused. Chrome
  bridge flows get `flow.server_conn.via` set to the next proxy; mitmproxy then opens
  them with a CONNECT through it (`make_server_connection` passes `server_conn.via`).
- `src/config.rs` and `src/providers/anthropic/mod.rs`: `CCP_ANTHROPIC_PROXY` and
  `CCP_ANTHROPIC_PROXY_CA` give the Anthropic passthrough client an HTTPS proxy and an
  extra root. reqwest is built with webpki roots, so `SSL_CERT_FILE` would not work.
- `scripts/claude-account-router`: `serve()` sets both, and writes
  `upstream-ca.pem` (public roots plus the next CA) for mitmproxy's
  `ssl_verify_upstream_trusted_ca`.

## Verified

- Unit tests: `tests/account_inject_test.py` cascade cases. Python suite 128 passed,
  1 skipped. `cargo test --release -- --test-threads=1`: no failures.
- Live on ftower: a throwaway hop A on `127.0.0.1:18799` holding only an unusable test
  alias, chained to the live proxy at `100.103.162.14:18790`.
  - The profile lookup for a foreign alias returned that account's email.
  - An alias no hop holds got B's 401 `unknown account alias`.
  - A real `claude -p` from dev over an ssh forward to A answered `cascade ok`. B's
    log showed no quota fallback for it.
- A curl `/v1/messages` was served by Codex: B fell back on its own, which is expected
  for a non-Claude-Code client. A did not.

## Gotcha found

SIGTERM to the launcher alone leaves the router and mitmdump running: Python's
default SIGTERM handler skips `serve()`'s `finally`. systemd kills the whole cgroup,
so the ftower service is unaffected. Test runs must stop the children too.
