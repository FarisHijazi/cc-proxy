# 2026-10-28: claude_rc moves out to its fork (pinned git dependency)

- CURRENT: `claude_rc` is no longer vendored. `python/claude_rc`, `tests/claude_rc*_test.py`, the TTS fixture and
  `scripts/claude-rc-smoke` were removed; they live in `FarisHijazi/claude-rc-api` (`~/Projects/claude-rc-api`),
  merged there onto upstream 0.4.0 and renumbered 0.5.0. See that repo's `CLAUDE.md` and
  `docs/devlog/claude_2026-10-28-canonical-fork.md`.
- `pyproject.toml` depends on `claude-rc-api[web,cli] @ git+https://github.com/FarisHijazi/claude-rc-api@<sha>`
  (exact commit). The `web` extra and the `claude-rc` script entry are gone here: the dependency installs both.
- Why a dependency and not a synced copy: nothing in `python/cc_proxy` imports `claude_rc`, so one pinned line is
  the whole coupling. The only reverse link is the cswap-slot reader, which compiles `slot_token` out of
  `scripts/account_inject.py`; the fork now finds it through the `cc_proxy` package (wheel `cc_proxy/scripts` or
  the editable checkout's `scripts/`).

Verified:
- `uv run --with pytest --with mcp pytest -q tests --ignore tests/account_artifacts_test.py`: 149 passed, 2
  skipped. `account_artifacts_test.py` needs `claude_artifact_cli`, which is not installed here; it does not touch
  claude_rc.
- The fork's 265 tests pass against the installed dependency in this venv, the cswap-slot test included
  (`account_inject.py` found through the editable cc_proxy).
- Live, read-only, cookie auth over dev Chrome CDP through the installed `claude-rc`: `whoami` (org present) and
  `list --limit 2`.

Updating: change the fork, push `main`, then bump the sha in `pyproject.toml` and re-run the tests above.
