"""Standalone Codex maintenance uses upstream's updater, without touching sessions."""
import fcntl
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
UPDATER = ROOT / "scripts/codex-update"
INSTALLER = ROOT / "scripts/install-codex-updater"


@pytest.fixture
def installation(tmp_path):
    home = tmp_path / "home"
    target = home / ".codex/packages/standalone/releases/toy/bin/codex"
    target.parent.mkdir(parents=True)
    target.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'printf "%s\\n" "$1" >> "$HOME/calls"\n'
        'case "$1" in\n'
        '  --version) printf "codex-cli %s\\n" "$(cat "$HOME/version")" ;;\n'
        '  update)\n'
        '    if [[ "${UPDATE_FAIL:-0}" != 0 ]]; then echo "update failed" >&2; exit 7; fi\n'
        '    if [[ -n "${UPDATE_TO:-}" ]]; then printf "%s\\n" "$UPDATE_TO" > "$HOME/version"; fi\n'
        '    ;;\n'
        '  *) exit 8 ;;\n'
        'esac\n'
    )
    target.chmod(0o755)
    executable = home / ".local/bin/codex"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(target)
    (home / "version").write_text("0.1.0\n")
    env = {**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"}
    for key in ("CODEX_HOME", "CC_PROXY_CODEX_BIN", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        env.pop(key, None)
    return home, env


def run_update(env):
    return subprocess.run(["bash", str(UPDATER)], env=env, text=True, capture_output=True, timeout=10, check=False)


def test_current_install_is_noop(installation):
    home, env = installation
    result = run_update(env)
    assert result.returncode == 0, result.stderr
    assert "Codex is current: codex-cli 0.1.0" in result.stdout
    assert (home / "calls").read_text().splitlines() == ["--version", "update", "--version"]
    assert (home / "version").read_text() == "0.1.0\n"


def test_updated_version_is_reported(installation):
    home, env = installation
    result = run_update({**env, "UPDATE_TO": "0.2.0"})
    assert result.returncode == 0, result.stderr
    assert "codex-cli 0.1.0 -> codex-cli 0.2.0" in result.stdout
    assert "running sessions were not restarted" in result.stdout
    assert (home / "version").read_text() == "0.2.0\n"


def test_upstream_failure_is_not_reported_as_success(installation):
    home, env = installation
    result = run_update({**env, "UPDATE_FAIL": "1"})
    assert result.returncode == 7
    assert "update failed" in result.stderr
    assert "Codex is current" not in result.stdout and "Codex updated" not in result.stdout
    assert (home / "version").read_text() == "0.1.0\n"


def test_concurrent_run_skips_without_invoking_codex(installation):
    home, env = installation
    lock = home / ".local/state/cc-proxy/codex-update/lock"
    lock.parent.mkdir(parents=True)
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_update(env)
    assert result.returncode == 0
    assert "another updater is running" in result.stdout
    assert not (home / "calls").exists()


def test_missing_binary_fails_loudly(installation):
    home, env = installation
    result = run_update({**env, "CC_PROXY_CODEX_BIN": str(home / "missing")})
    assert result.returncode == 1
    assert "executable not found" in result.stderr
    assert not (home / "calls").exists()


def test_unmanaged_binary_is_not_updated(installation):
    home, env = installation
    unmanaged = home / "other-codex"
    unmanaged.write_text("#!/bin/sh\nexit 0\n")
    unmanaged.chmod(0o755)
    result = run_update({**env, "CC_PROXY_CODEX_BIN": str(unmanaged)})
    assert result.returncode == 1
    assert "not a standalone Codex installation" in result.stderr
    assert not (home / "calls").exists()


def test_installer_installs_only_updater_and_enables_hourly_timer(installation):
    home, env = installation
    mock_bin = home / "mock-bin"
    mock_bin.mkdir()
    systemctl = mock_bin / "systemctl"
    systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/systemctl-calls"\n')
    systemctl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(INSTALLER)], env={**env, "PATH": str(mock_bin) + ":/usr/bin:/bin"},
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (home / ".local/libexec/cc-proxy/codex-update").read_bytes() == UPDATER.read_bytes()
    assert (home / "systemctl-calls").read_text().splitlines() == [
        "--user daemon-reload", "--user enable --now cc-proxy-codex-update.timer",
    ]
    units = home / ".config/systemd/user"
    assert "OnCalendar=hourly" in (units / "cc-proxy-codex-update.timer").read_text()
    service = (units / "cc-proxy-codex-update.service").read_text()
    assert "TimeoutStartSec=15min" in service
    assert not (home / "calls").exists()
