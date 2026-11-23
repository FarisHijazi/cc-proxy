# Traffic recorder moved off mitmproxy's event loop (2026-11-12)

## Symptom
Remote Control from the work-a and work-b sessions kept dropping ("Transport recovery exhausted (code 4091)",
"Session creation failed", "Remote credentials fetch failed"). Over 20 minutes, 43 of 52 `worker/events` POSTs from
two work-a sessions ended "Client disconnected", at 3-10 s each. Claude Code gives up at about 10 s. The hub's own
mirror calls from the proxy host took 0.5 s, and that host to api.anthropic.com took 0.34 s.

## Cause
`account_traffic.py` did all its work inside mitmproxy hooks: decoding, JSON parsing, zlib, blob dedup inserts,
the hourly prune `DELETE`s and the `INSERT`. It also ran a `tailscale status` subprocess (5 s timeout) every 5 min.
Those hooks run on the single event loop that serves every connection, so each disk wait stalled all traffic. The
DB had reached 18 GB (`traffic_days` unset). `mitmdump` was seen in D state, and a local `/health` took 3 s once.
The same class of bug as the token fetch fixed earlier today (`claude_20261112-proxy-freeze-and-all-chromes.md`).

## Fix
- Hooks copy plain data (raw bytes, header lists, ids) into a `queue.Queue(QUEUE_MAX=5000)`, with `put_nowait`.
  When it is full the row is dropped and `dropped` counts it.
- One daemon thread (`traffic-db`) owns the connection and does everything else, including `tailnet_name`.
- `flush()` waits for the queue (tests).
- Tests: `test_a_slow_database_never_blocks_the_event_loop`, `test_a_full_queue_drops_rows_instead_of_waiting`.
  Full suite: 267 passed (`HOME=$(mktemp -d) uvx --with mitmproxy --with git+https://github.com/FarisHijazi/claude-swap
  pytest tests/ --ignore=tests/account_artifacts_test.py`; that test needs `claude_artifact_cli`).

## Deployed on the proxy host
- The old DB was moved, not deleted, to `~/.local/state/claude-codex/traffic-before-rotate-20261112/` (18 GB).
  A fresh `traffic.sqlite3` started at 09:22 EST.
- `scripts/account_traffic.py` was copied (backup `account_traffic.py.before-writer-thread-20261112`), and
  `claude-account-proxy` was restarted at 09:30 EST.
- After: 0 of 299 `worker/events` POSTs failed. A relayed client averaged 0.3 s (was 3.3 s), local ones 0.27 s,
  and remote work machines 1.7-2.4 s with no timeouts. Across all `/v1/code/sessions/cse_*` calls, 2 of ~750 failed.

## Open
- `traffic_days` is still unset, so the new DB will grow without limit again. The writer thread keeps that off the
  request path, but it still fills the disk.
- One client reaches the proxy host only through a Tailscale relay (~215 ms): its NAT has
  `MappingVariesByDestIP: true`.
