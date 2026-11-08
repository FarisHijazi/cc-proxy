# Main-only native Chrome and artifacts

Changed `scripts/claude-account-router` so the default native Chrome placeholder routes to the main login rather than aggregating every account, while an explicit `--chrome-account` remains supported. The `account-artifacts` manifest now contains only the main account, and copies only its secure-storage directory. Inference account discovery and fallback are unchanged.

Updated `tests/account_router_test.py` to assert the main-only default Chrome placeholder and artifact child set. Independent review caught an explicit-selector regression: direct mode also consumed the artifact manifest. Explicit Chrome selection now gets its own `chrome-accounts.json`, preserving the selected target without exposing it to artifacts. The regression test configures the real `NativeChromeRoute` against this file.

Verification: all 56 account-router tests pass. The combined Python suite excluding the separate artifacts adapter suite passed 255 tests; focused launcher, traffic and usage tests including the added connection-error test passed 115 tests. Independent review found no remaining confirmed regressions.

After explicit approval, deployed the launcher to the shared proxy and affected laptop. Backups are under `~/.local/state/claude-codex/before-main-only-20261108/`. Stopped only the affected session's inspected Claude PID and resumed its exact conversation ID in the same tmux pane. Its native `/chrome` picker now lists four browsers, two macOS and two Linux. The corresponding upstream roster contains the known personal and work-browser profiles on the laptop, plus the two Linux browsers already attached to main, all with native IDs rather than exported aggregation IDs. Remote Control reconnected, and the session was returned to its prompt. Earlier dev-only browser checks were not proof about that session.

Future scope requested by the owner: Kubernetes HA. No cluster changes were made; account refresh ownership, session stickiness, WebSocket lifecycle and traffic persistence need an HA design before adding replicas.
