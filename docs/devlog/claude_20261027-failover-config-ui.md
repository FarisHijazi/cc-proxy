# Configurable failover and its dashboard editor

The tier chain in `src/fallback.rs` was hardcoded and the dashboard could only reorder accounts.
Now the accounts file's optional `fallback` section configures it, and the dashboard edits it.

## Config (accounts file, single source of truth)

```json
"fallback": {
  "chains": {
    "claude": ["accounts", "codex", "cursor"],
    "gemini": ["gemini", "claude-sonnet-5", "cursor-cli"]
  },
  "on": {"rate_limit": true, "auth": true, "server_error": false},
  "threshold": 90
}
```

- `chains` is keyed by model family: `claude` (the anthropic provider) or the provider name
  (`codex`, `gemini`, `cursor`, `cursor-cli`, ...). A missing family keeps the old default
  (`claude`: accounts, codex, cursor; `codex`: codex, accounts, cursor; others: no failover).
- Targets: `accounts` = each Claude account (the requested model for `claude`, else the tier
  equivalent); `codex` / `cursor` = the tier equivalent on that backend; the family's own name =
  the requested model untouched; anything else = that exact model id (a `claude-*` id runs on each
  account). Tier keywords are skipped for models without a tier (gemini, cursor-cli).
- `on`: which failures move on. `server_error` (5xx) is off by default and never sets a cooldown.
- `threshold` (percent, absent = off): accounts whose latest reply reported 5h or 7d utilization at
  or above it go to the end of the pool until that window's reset.

## Owners

- `account_inject.py` reads the file (it already did), attaches the section as
  `x-claude-codex-fallback` next to the pool on `POST /v1/messages`, drops any client-supplied copy,
  and alone applies `threshold`: `responseheaders` records `anthropic-ratelimit-unified-{5h,7d}-*`
  for the alias in `x-claude-codex-served-by`, and `pool()` moves hot accounts last. The `primary`
  marker does not move, so a thread delta is still refused rather than sent to another account.
- `src/fallback.rs` builds the chain from the policy. `take_pool` removes both headers before
  anything is captured or relayed, and the Anthropic relay strips both as a second guard. The
  no-retry-after-output and thread-ownership rules are unchanged.
- `account_ui.py` validates and writes `fallback` (whitelisted like the other keys, `null` removes
  it), and lists providers from the router's `/v1/models`, fetched through the `--serve` proxy
  (every api.anthropic.com path goes to the router, whose port is random).
- `account_ui.html`: Providers row, a per-family chain editor (drag to reorder, add from shortcuts
  or listed model ids, remove, reset to default) and a triggers panel with the threshold slider.
  While the router is unreachable it says so instead of marking every backend unavailable.

## Verification

- Rust: 877 library tests plus every integration target passed with an empty `HOME`
  (`account_fallback`: 15, three new: configured order, 5xx toggle without cooldown, 429 toggle off);
  `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings` clean.
- Python: 161 passed, 2 skipped (new: policy header ownership, threshold demotion and reset expiry,
  bad threshold refused, fallback save/clear and validation, provider listing).
- ftower: backups `*.before-failover-ui` (binary, three scripts, accounts.json); binary SHA-256
  `90a0cb9180c07fca3ce8cf34fcde50b3d92b7b376c9f006139382ccd5d13b3ae`; one restart of
  `claude-account-proxy.service` and `cc-proxy-ui.service`. `claude-accounts -p` returned `PONG` for
  `claude-haiku-5-5` before and with a saved `fallback` section, and for `gpt-6-luna`. The live
  `/api/state` listed anthropic 12, codex 26, cursor 8, cursor-cli 3, gemini 6 models; kimi and
  grok are not signed in. The test section was then removed (`fallback: null`), so live routing is
  still the default.
