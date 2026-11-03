# Every router model in the dashboard and the /model picker

## Problem

The dashboard's Models page showed only the three equivalence groups. Those groups say which model substitutes for
which during failover. They are not the list of models. The full list (336 ids on 6 backends) appeared only as a hover
title on the backend chips. In Claude Code, `/model` showed only the launcher's 8 family rows plus rows typed by hand into
a host's `~/.claude/settings.json`. That is why gemini and cursor appeared on one Mac, but a new local qwen model did not.

## Change

The Models page now starts with "Available models". It has one card per backend listing every id, a filter box, and
click-to-copy. The cursor-agent card (273 ids) starts collapsed unless the filter is set.
It renders nothing until the first `/api/state` settles. An unreachable router says so instead of showing an empty list.

`scripts/claude-account-router` gains `served_models(proxy_url, ca)`. At launch it reads `/v1/models` through the proxy.
`model_picker()` then appends every id whose backend is not `anthropic`, because Claude Code lists Claude models itself.
It skips the `cursor-cli:*` per-effort variants and labels each row `id (backend)`.
Saved rows come first and keep their labels. If the router can't be reached within 15 s, the launcher prints one line
and the picker keeps its defaults.

## Checks

The test in `tests/account_ui_providers_test.mjs` (catalog lists every id, filter, loading state) and the two in
`tests/account_router_test.py` failed before the change and pass after. The Python suite gives 244 passed, and all DOM tests pass.
The live dashboard at 1400 px and 390 px lists 336 ids, has no overflow and no script errors. A throwaway session on
dev showed 71 picker rows ending in `qwen3.8-27b-abliterated (local)`. The first throwaway launch got the defaults only.
A cold router cache is the likely cause, since the router enumerates cursor-agent models, so the timeout went from 5 s to 15 s.
The launchers on the other three client hosts were patched in place, with backups in
`~/.local/state/claude-codex/before-router-picker-20261102/`. Each produced 62 rows with qwen through its own proxy route.
Open sessions keep their old picker until they restart.
