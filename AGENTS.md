# Fork practices

- Stay close to upstream and avoid style-only divergence.
- Preserve the `claude-codex` crate, binary, and release identity.
- Keep Claude aliases on Anthropic by default and explicit GPT models on Codex.
- Use Codex CLI authentication. Keep automated tests on toy credentials and mock upstreams.
- Preserve unrelated work and run locked format, clippy, and test checks before release.
- Publish releases to crates.io and tag `vX.Y.Z` for prebuilt GitHub binaries.
- `scripts/claude-account-router` delegates session-scoped forward proxying to
  mitmproxy; `src/server.rs` remains the model routing owner. Never stream
  request bodies before mitmproxy's `map_remote` hook runs. The vendored
  response-only addon and license are pinned upstream copies.
- Account launcher credentials are access-only snapshots in temporary secure
  storage. Never copy or consume the source login's refresh grant, and never
  write its config directory. Read the account-routing devlog when changing
  this path (deliberately a plain path, not an `@` include):
  `docs/devlog/claude_20261018-account-inference-router.md`.
- Launcher checks: `uvx pytest tests/account_router_test.py`, with `mitmdump`
  installed, and `uvx ruff check scripts/claude-account-router tests/account_router_test.py`.
- Unknown account endpoints use the Anthropic provider's shared relay, never a
  model backend or a caller-selected origin. Preserve HTTP frames/trailers,
  compression, cookies, WebSocket upgrades and the inbound-auth boundary.
  Keep proxy credentials local. `--start-router` owns only its child processes;
  never stop an existing server to run a session. Read the relay record on demand:
  `docs/devlog/claude_20261018-account-endpoint-relay.md` (plain path, not an include).
- Stateless providers must refuse `thread:continue` with the recognized
  `thread_unsupported_request` envelope so Claude resends full history. Keep
  Anthropic threads and token counting untouched; do not accept contextless deltas.
- Multi-account tools extend this launcher: main owns Remote Control and native
  `/chrome`; `account-chrome` multiplexes persistent native MCP children and
  `account-artifacts` reuses the published artifact CLI for read-only access.
  Never route a failed account's action to another account. Preserve vendor
  notifications, native permissions, and full platform trust in the session CA
  bundle. Read `docs/devlog/claude_20261018-multi-account-tools.md` on demand
  (plain path, not an include). Run all three `tests/account_*_test.py` suites
  with pinned FastMCP/artifact CLI dependencies documented there.
