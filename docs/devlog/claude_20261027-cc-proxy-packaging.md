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
