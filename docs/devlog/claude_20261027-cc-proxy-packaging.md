# Packaging cc-proxy: uvx, Docker, services

## One-line install

`uvx --from git+https://github.com/FarisHijazi/cc-proxy cc-proxy`. `pyproject.toml` (hatchling)
ships `scripts/` unchanged inside the wheel as `cc_proxy/scripts`, so deployed machines that run
`scripts/claude-account-router` by path keep working. `python/cc_proxy/cli.py` is the entry
point (`cc-proxy`, `cc-proxy-traffic`). It puts this environment's bin dir on PATH for
`mitmdump`, and for `--serve` and local mode it fetches `claude-codex-<os>-<arch>.tar.gz` from
release `v<package version>`, verifies the `.sha256`, and caches it under
`~/.cache/cc-proxy/<tag>/`. These are the release-workflow assets `scripts/install.sh` uses.
`--via` clients and `--help` never download it.

Not on PyPI: PyPI rejects direct git dependencies, and claude-swap is the FarisHijazi fork on
GitHub only. The name `cc-proxy` is free on PyPI (checked 2026-10-27).

Package and crate versions move together (0.4.0), because the launcher downloads the router
from the release tag that matches its own version.

## Docker

`Dockerfile`: a Rust stage builds `claude-codex`, a python:3.12-slim stage installs the package
with uv. `compose.yaml` mounts the accounts file, the claude-swap store, `~/.claude` and
`~/.claude.json` read-only, `~/.codex`, and a state volume (CA, traffic database). It publishes
on `CC_PROXY_BIND` (default loopback), because the proxy has no auth.

## Verified

- The wheel builds. `uvx --from <wheel>` ran `--help`, `cc-proxy-traffic --help`, and a real
  `--via` launch through the ftower proxy that replied.
- The image builds, and has `mitmdump`, `claude-codex 0.4.0` and `claude_swap`. A container
  chained to ftower (`--serve --via`) answered a profile lookup through it.
- README service snippets: the systemd unit passes `systemd-analyze --user verify`, and the
  launchd plist passes `plutil -lint` on the Mac. Both were written into a throwaway HOME;
  neither was installed.
- Not yet verified: the router download, because release v0.4.0 does not exist yet.

## Release CI

The first `v0.4.0` release run failed in `cargo test`: `models_endpoint_lists_supported_models`
expected `gpt-5.6-sol`, but the router lists Codex models only when a Codex login exists, and CI
has none. It passed on dev only because dev has `~/.codex/auth.json`. The test now writes its own
login and points `CCP_CODEX_AUTH_FILE` at it. Reproduce CI locally with an empty `HOME` (keep
`CARGO_HOME` and `RUSTUP_HOME`).
The second run built Linux and macOS, but Windows failed: `is_executable_file` in
`src/providers/cursor_cli/process.rs` used `std::os::unix` without a `#[cfg(unix)]` guard. It now
has a Unix version (execute bit) and a non-Unix one (any file).

## Faster, config-driven client launch (claude-accounts moves here)

`claude-accounts` was a dotclaude shell wrapper that probed the proxy with an HTTP GET (`curl -m 2`,
1.2 s on a healthy tailnet) and fell back to a wstunnel tunnel. Under load the GET passed 2 s, so
machines on the tailnet wrongly took the tunnel. The launcher then fetched each account's profile
in series (0.5 s each).

Now `scripts/claude-accounts` (and the package's `claude-accounts` entry) runs the launcher with
`~/.config/claude-codex/accounts.json`. Its `via` list and `via_ca` replace `--via`/`--via-ca`.
`reach()` tries each entry with a 0.4 s TCP connect. A `tunnel` entry starts wstunnel for the session
and is ready when its local port listens. `profiles()` caches profiles for a day in
`<state>/profiles.json` and fetches missing ones in parallel. A single explicit `--via` is used as
given.

Measured with `claude --version` as the payload (the launcher's own cost):

| Machine | Before | First launch | Cached |
| --- | --- | --- | --- |
| dev (tailnet) | about 2.9 s | 1.49 s | 0.44 s |
| work VM (tailnet) | not measured | 2.82 s | 0.76 s |
| second work VM (tunnel only) | not measured | 2.89 s | 0.68 s |

The Mac keeps the old dotclaude wrapper until its checkout can be updated.
