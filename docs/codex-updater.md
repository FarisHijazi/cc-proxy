# Hourly Codex updates

On the Linux machine running the shared proxy, install this timer from the cc-proxy checkout:

```sh
bash scripts/install-codex-updater
systemctl --user start cc-proxy-codex-update.service
systemctl --user list-timers cc-proxy-codex-update.timer
journalctl --user -u cc-proxy-codex-update.service -n 30
```

The timer runs hourly and catches up after downtime. The user's systemd manager must remain running after logout (`loginctl enable-linger "$USER"`, as for the proxy service).

`scripts/codex-update` calls upstream's existing `codex update`; upstream owns release discovery, downloads, its install lock and standalone release selection. No parallel downloader, release cache or package manager is added. The default executable is `$HOME/.local/bin/codex`; it must resolve inside `$CODEX_HOME/packages/standalone/releases/` (`CODEX_HOME` defaults to `$HOME/.codex`). A different executable can be set with `CC_PROXY_CODEX_BIN` in a systemd service drop-in.

The wrapper serializes manual/timer runs using `flock`, logs the before/after version, propagates upstream failures, and reports a current version as a no-op. The service times out after 15 minutes. It does not restart the proxy or any running Codex/Claude session and does not read or change login files. The installed CLI and future processes use the updated standalone release; already-running CLI processes keep their loaded executable.

The updater installs into `~/.local/libexec/cc-proxy/` and creates `cc-proxy-codex-update.service` / `.timer` under the systemd user unit directory. Re-run the installer after modifying the script or units. No timer is installed on the laptop by these commands when run on the proxy host.

Disable automatic updates without deleting the installation:

```sh
systemctl --user disable --now cc-proxy-codex-update.timer
```

Tests: `pytest -q tests/codex_update_test.py` (toy standalone installation; no network, real accounts, or live service changes).
