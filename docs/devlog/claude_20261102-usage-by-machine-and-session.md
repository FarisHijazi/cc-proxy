# Usage by machine and session

## What it does

The dashboard has a Usage view (`#usage`). For 24 hours, 7, 30 or 90 days it shows total model calls and tokens.
It then breaks these down by machine, by answering account and by session, each sorted by tokens. Clicking a machine lists only
that machine's sessions. A session row shows its folder, the accounts that answered it, and how long it was active.

## How

`account_traffic.py` is the traffic database's only writer. It now stores `input_tokens` (input plus cache
creation), `cache_read_tokens` and `output_tokens` on every model-call row. It parses them from the reply with
`account_providers.reply_tokens`, which the provider cards already used. A reply that reported no usage keeps
NULL, and the view shows "tokens unknown" for it, never zero. Older databases gain the columns when the recorder opens
them. `claude-traffic backfill-tokens` fills rows recorded before the change and is safe to rerun.
`account_ui.py` serves `/api/usage?hours=N` (1 hour to 90 days, top 300 sessions) from those columns. It does not decompress any bodies.
Token-counting calls are excluded.

Grid items with `margin: auto` shrink to their content, so `main` had grown to its widest table. At phone width every view
scrolled sideways. `main` now has `width: 100%; min-width: 0`, and wide tables scroll inside their panels.

## Checks

New tests are in `tests/account_traffic_test.py` (stored counts, unknown stays NULL, column migration, backfill) and
`tests/account_ui_test.py` (per machine, session and account totals, a database without the columns). The tests failed before the change.
The Python suite, excluding the artifact test that needs FastMCP, gives 242 passed. The four DOM tests pass.

The change is deployed on the shared proxy host with backups in `~/.local/state/claude-codex/before-usage-view-20261102/`. The proxy
addon hot-reloaded without a restart, and new rows carried token counts. The backfill filled 61,294 rows in 50 seconds. Real
Chromium screenshots of the live page at 1400 px and 390 px show no page overflow and no script errors, and the machine filter works.
