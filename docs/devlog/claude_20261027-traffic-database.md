# Recording all proxy traffic in SQLite

The `--serve` proxy now records every flow when its accounts file has `"traffic_db"`.

## Why a new recorder

- `CCP_TRAFFIC_LOG` (`src/traffic.rs`) writes loose JSON files, covers only the router's
  model requests, and knows nothing about the sending machine. `--serve` disables it.
- mitmproxy's own flow dump (`save_stream_file`) would store the real tokens that
  `account_inject.py` swaps in, and has no sender metadata.
- mitmproxy sees every flow on the shared proxy (inference, OAuth, usage, Remote
  Control, Chrome bridge), so the recorder lives there.

## Shape

- `scripts/account_traffic.py`: the `TrafficDB` addon. `account_inject.py` loads it first in
  its `addons`, so it sees each request before the placeholder is swapped, and so a
  live script reload picks it up without restarting the proxy. mitmproxy watches only
  `account_inject.py`: after changing `account_traffic.py`, rewrite `account_inject.py` too.
- Sender: `claude-account-router --via` sets `ANTHROPIC_CUSTOM_HEADERS` with
  `X-Claude-Client-Host`, `-User` and `-Cwd` (percent-encoded). The recorder strips them.
  Without them, `tailscale status --json` names the client IP.
- The working directory comes from Claude Code's environment block, which newer versions
  send as a `system`-role message.
- Served by: the router now sets `x-claude-codex-served-by: <account or backend>/<model>`
  (`src/fallback.rs`).
- Redaction: credential headers and the JSON keys in `SECRET_KEYS`. Token counts such as
  `output_tokens` are kept.
- Size: `system`, `tools` and each message go into `blobs` once, keyed by SHA-256. Live,
  this cut growth from about 1.9 MB a minute to about 0.3 MB a minute.

## Verified

- Suite: 136 passed, 1 skipped (`tests/account_traffic_test.py`). Rust tests pass.
- Live on ftower: rows for dev, the work VM and ftower. A headless launch on the work VM
  recorded its host, user, launch folder, pinned placeholder, model and status.
- `served_by` stays empty until the proxy service is restarted onto the new router binary
  (installed, not restarted, so running sessions were not interrupted).

## Retention

`"traffic_days"` in the accounts file. `TrafficDB.prune()` runs at most once an hour from
`record()`, so no separate job exists. It deletes flows and websocket messages older than the
cutoff, and blobs whose `last_seen` is older. A blob a kept flow uses was seen when that flow
was recorded, so it survives. Older databases gain `last_seen` on connect, set to now. ftower
keeps 30 days.
