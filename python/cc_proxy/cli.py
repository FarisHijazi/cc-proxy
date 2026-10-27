"""cc-proxy entry points: the account launcher in scripts/, with its tools on PATH.

The launcher runs `mitmdump` and the Rust `claude-codex` router by name. mitmdump comes
from this Python environment. claude-codex is used from PATH when present, otherwise
downloaded once from this version's GitHub release and checked against its .sha256,
the same files scripts/install.sh uses.
"""

import hashlib
import io
import os
import platform
import runpy
import shutil
import sys
import tarfile
import urllib.request
from importlib.metadata import version
from pathlib import Path

REPO = "FarisHijazi/cc-proxy"
SCRIPTS = Path(__file__).with_name("scripts")
OS = {"Darwin": "darwin", "Linux": "linux"}
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def router_dir():
    """A directory holding claude-codex, or None when it is already on PATH."""
    if shutil.which("claude-codex"):
        return None
    tag = "v" + version("cc-proxy")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "cc-proxy" / tag
    if (cache / "claude-codex").exists():
        return cache
    system, machine = OS.get(platform.system()), ARCH.get(platform.machine().lower())
    if not (system and machine):
        sys.exit(f"cc-proxy: no prebuilt router for {platform.system()} {platform.machine()}; "
                 "put claude-codex on PATH (cargo install --git https://github.com/" + REPO + ")")
    name = f"claude-codex-{system}-{machine}"
    base = f"https://github.com/{REPO}/releases/download/{tag}/"
    print(f"cc-proxy: downloading the router ({name}, {tag})", file=sys.stderr)
    archive = fetch(base + name + ".tar.gz")
    expected = fetch(base + name + ".sha256").split()[0].decode()
    if hashlib.sha256(archive).hexdigest() != expected:
        sys.exit(f"cc-proxy: checksum mismatch for {name}.tar.gz")
    cache.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extract("claude-codex", cache, filter="data")
    (cache / "claude-codex").chmod(0o755)
    return cache


def run(script):
    path = [str(Path(sys.executable).parent)]
    args = sys.argv[1:]
    # --via clients and --help never start a router.
    needs_router = "--serve" in args or not ({"--via", "-h", "--help"} & set(args))
    if script == "claude-account-router" and needs_router:
        found = router_dir()
        if found:
            path.insert(0, str(found))
    os.environ["PATH"] = os.pathsep.join([*path, os.environ.get("PATH", "")])
    sys.argv[0] = script
    runpy.run_path(str(SCRIPTS / script), run_name="__main__")


def main():
    run("claude-account-router")


def traffic():
    run("claude-traffic")


def ui():
    run("account_ui.py")


def accounts():
    """claude-accounts: the launcher with ~/.config/claude-codex/accounts.json (or $CC_PROXY_ACCOUNTS)."""
    default = Path.home() / ".config/claude-codex/accounts.json"
    sys.argv[1:1] = ["--accounts-file", os.environ.get("CC_PROXY_ACCOUNTS") or str(default)]
    run("claude-account-router")
