# Fork practices

- Stay close to upstream and avoid style-only divergence.
- Preserve the `claude-codex` crate, binary, and release identity.
- Keep Claude aliases on Anthropic by default and explicit GPT models on Codex.
- Use Codex CLI authentication. Keep automated tests on toy credentials and mock upstreams.
- Preserve unrelated work and run locked format, clippy, and test checks before release.
- Publish releases to crates.io and tag `vX.Y.Z` for prebuilt GitHub binaries.
- `scripts/claude-account-router` delegates inference-only forward proxying to
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
