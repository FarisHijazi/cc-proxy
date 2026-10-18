"""Run with uvx pytest tests/account_router_test.py (mitmdump required)."""

import json
import re
import runpy
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

launcher = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts/claude-account-router")
)


def test_routing_boundary_and_loopback_only(tmp_path):
    command = launcher["proxy_command"]("http://127.0.0.1:18765", 19999, tmp_path)
    delimiter, method, pattern, replacement = command[-1].split("@")
    assert delimiter == "" and method == "~m POST"
    for suffix in (
        "/v1/messages",
        "/v1/messages?beta=true",
        "/v1/messages/count_tokens",
    ):
        assert (
            re.sub(pattern, replacement, "https://api.anthropic.com" + suffix)
            == replacement + suffix
        )
    for url in (
        "https://api.anthropic.com/api/oauth/usage",
        "https://api.anthropic.com/v1/messages/other",
        "https://api.anthropic.com/v1/messages/count_tokens/other",
        "https://api.anthropic.com.evil.test/v1/messages",
        "https://claude.ai/api/organizations",
    ):
        assert re.sub(pattern, replacement, url) == url
    for router in (
        "https://example.com",
        "http://user:pass@localhost",
        "http://localhost/path",
        "http://localhost/?secret=1",
    ):
        with pytest.raises(ValueError):
            launcher["proxy_command"](router, 19999, tmp_path)


def test_login_snapshot_excludes_refresh_and_leaves_source_intact(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    secure = tmp_path / "secure"
    secure.mkdir()
    oauth = {
        "accessToken": "toy-access",
        "refreshToken": "toy-refresh",
        "scopes": ["user:inference", "user:profile"],
        "expiresAt": 123,
    }
    credential = source / ".credentials.json"
    credential.write_text(
        json.dumps({"claudeAiOauth": oauth, "otherSecret": "never-copy"})
    )
    original = credential.read_bytes()
    (source / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": "user@example.com"},
                "hooks": "never-copy",
            }
        )
    )
    monkeypatch.setattr(launcher["sys"], "platform", "linux")
    launcher["bootstrap_login"](source, tmp_path / "client", secure)
    data = json.loads((secure / ".credentials.json").read_text())
    assert data == {
        "claudeAiOauth": {k: v for k, v in oauth.items() if k != "refreshToken"}
    }
    assert credential.read_bytes() == original
    assert (secure / ".credentials.json").stat().st_mode & 0o777 == 0o600
    assert "hooks" not in (tmp_path / "client/.claude.json").read_text()


def test_keychain_precedes_plaintext(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    secure = tmp_path / "secure"
    secure.mkdir()
    (source / ".credentials.json").write_text(
        '{"claudeAiOauth":{"accessToken":"stale"}}'
    )
    monkeypatch.setattr(launcher["sys"], "platform", "darwin")
    monkeypatch.setattr(
        launcher["subprocess"],
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            [], 0, '{"claudeAiOauth":{"accessToken":"fresh","refreshToken":"private"}}'
        ),
    )
    launcher["bootstrap_login"](source, tmp_path / "client", secure)
    data = json.loads((secure / ".credentials.json").read_text())
    assert data == {"claudeAiOauth": {"accessToken": "fresh"}}


def test_client_has_first_party_url_and_isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://old-router:1")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "toy-gateway")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "/do-not-write")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "123")
    monkeypatch.setenv("CLAUDE_CODE_HOST_CREDS_FILE", "/do-not-read")
    monkeypatch.setenv("CLAUDE_CODE_ACCOUNT_UUID", "wrong-account")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ACCESS_TOKEN", "wrong-session")
    monkeypatch.setenv("SESSION_INGRESS_URL", "https://wrong.test")
    env, serialized = launcher["client_environment"](
        tmp_path, 19999, tmp_path / "secure"
    )
    settings = json.loads(serialized)
    assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:19999"
    assert env["SSL_CERT_FILE"] == env["NODE_EXTRA_CA_CERTS"]
    assert env["REQUESTS_CA_BUNDLE"] == env["SSL_CERT_FILE"]
    assert env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(tmp_path / "secure")
    assert settings["env"]["ANTHROPIC_BASE_URL"] == env["ANTHROPIC_BASE_URL"]
    assert settings["disableAllHooks"] is True
    assert "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR" not in env
    assert "CLAUDE_CODE_HOST_CREDS_FILE" not in env
    assert "CLAUDE_CODE_ACCOUNT_UUID" not in env
    assert "CLAUDE_CODE_SESSION_ACCESS_TOKEN" not in env
    assert "SESSION_INGRESS_URL" not in env


def test_metadata_refreshes_when_source_account_changes(tmp_path):
    metadata = tmp_path / ".claude.json"
    launcher["update_account_metadata"](
        metadata, {"oauthAccount": {"accountUuid": "account-a"}}
    )
    launcher["update_account_metadata"](
        metadata, {"oauthAccount": {"accountUuid": "account-b"}}
    )
    assert (
        json.loads(metadata.read_text())["oauthAccount"]["accountUuid"] == "account-b"
    )
    assert metadata.stat().st_mode & 0o777 == 0o600


def test_protected_state_symlinks_are_rejected(tmp_path):
    protected = Path.home() / ".claude"
    (tmp_path / "claude").symlink_to(protected, target_is_directory=True)
    script = Path(__file__).parents[1] / "scripts/claude-account-router"
    result = subprocess.run(
        [str(script), "--state-dir", str(tmp_path), "--", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "must be outside ~/.claude" in result.stderr


def test_cswap_selector_reads_without_switching_or_refreshing(tmp_path, monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    (tmp_path / "configs").mkdir()
    sequence = {
        "activeAccountNumber": "9",
        "accounts": {
            "1": {
                "email": "user@example.com",
                "uuid": "toy-account",
                "organizationUuid": "toy-org",
            }
        },
    }
    path = tmp_path / "sequence.json"
    path.write_text(json.dumps(sequence))
    original = path.read_bytes()
    calls = []
    credential_store = SimpleNamespace(
        _read_account_credentials=lambda slot, email: (
            calls.append((slot, email))
            or {
                "accessToken": "toy",
                "refreshToken": "never-copy",
                "scopes": ["user:profile", "user:inference"],
            }
        )
    )
    package = ModuleType("claude_swap")
    package.oauth = SimpleNamespace(
        extract_oauth_data=lambda raw: raw, is_oauth_token_expired=lambda _: False
    )
    credentials = ModuleType("claude_swap.credentials")
    credentials.CredentialStore = lambda _: credential_store
    models = ModuleType("claude_swap.models")
    models.Platform = SimpleNamespace(detect=lambda: "toy")
    paths = ModuleType("claude_swap.paths")
    paths.get_backup_root = lambda: tmp_path
    for name, module in (
        ("claude_swap", package),
        ("claude_swap.credentials", credentials),
        ("claude_swap.models", models),
        ("claude_swap.paths", paths),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    secure = tmp_path / "secure"
    secure.mkdir()
    launcher["bootstrap_cswap"](1, tmp_path / "isolated", secure)
    assert calls == [("1", "user@example.com")]
    assert path.read_bytes() == original
    assert "refreshToken" not in (secure / ".credentials.json").read_text()
    assert (
        json.loads((tmp_path / "isolated/.claude.json").read_text())["oauthAccount"][
            "accountUuid"
        ]
        == "toy-account"
    )


@pytest.mark.parametrize("all_endpoints", [False, True])
def test_real_forward_proxy_routes_and_streams(tmp_path, all_endpoints):
    if not shutil.which("mitmdump"):
        pytest.skip("install mitmproxy to exercise the real forward proxy")
    records = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            records.append(
                (
                    self.server.label,
                    self.command,
                    self.path,
                    self.headers.get("Authorization"),
                    body,
                )
            )
            self.send_response(200)
            if self.path == "/v1/messages?stream=true":
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b"data: first\n\n")
                self.wfile.flush()
                release.wait(5)
                self.wfile.write(b"data: last\n\n")
            else:
                self.end_headers()
                self.wfile.write(self.server.label.encode())

        def log_message(self, *_args):
            pass

    router = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    router.label = "router"
    account = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    account.label = "account"
    for server in (router, account):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = launcher["proxy_command"](
        f"http://127.0.0.1:{router.server_port}", port, tmp_path, all_endpoints
    )
    # After the production rule, replace the remaining real host with the account
    # mock. Inference must already have left that host and cannot hit this rule.
    command += [
        "--map-remote",
        f"@!~m CONNECT@^https://api\\.anthropic\\.com@http://127.0.0.1:{account.server_port}",
    ]
    with (tmp_path / "proxy.log").open("w") as log:
        proxy = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            ca = tmp_path / "ca/mitmproxy-ca-cert.pem"
            for _ in range(200):
                assert proxy.poll() is None, (tmp_path / "proxy.log").read_text()
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        if ca.exists():
                            break
                except OSError:
                    pass
                time.sleep(0.05)
            curl = [
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--max-time",
                "10",
                "--noproxy",
                "",
                "--proxy",
                f"http://127.0.0.1:{port}",
                "--cacert",
                str(ca),
                "-H",
                "Authorization: Bearer toy-token",
            ]
            for method, path, label in (
                ("POST", "/v1/messages?beta=true", "router"),
                ("POST", "/v1/messages/count_tokens", "router"),
                ("GET", "/api/oauth/usage", "account"),
                ("POST", "/v1/sessions/test/events", "account"),
                ("POST", "/v1/messages/lookalike", "account"),
                ("GET", "/v1/messages", "account"),
            ):
                if all_endpoints:
                    label = "router"
                result = subprocess.run(
                    [
                        *curl,
                        "-X",
                        method,
                        "--data-binary",
                        '{"model":"toy"}',
                        "https://api.anthropic.com" + path,
                    ],
                    check=False,
                    capture_output=True,
                )
                assert result.returncode == 0, (
                    result.stderr.decode(),
                    (tmp_path / "proxy.log").read_text(),
                )
                assert result.stdout.decode() == label
                assert records[-1] == (
                    label,
                    method,
                    path,
                    "Bearer toy-token",
                    b'{"model":"toy"}',
                )
            stream = subprocess.Popen(
                [
                    *curl,
                    "--no-buffer",
                    "--data",
                    "{}",
                    "https://api.anthropic.com/v1/messages?stream=true",
                ],
                stdout=subprocess.PIPE,
            )
            try:
                assert stream.stdout.readline() == b"data: first\n"
                assert not release.is_set()  # First SSE delivered before completion.
                release.set()
                assert b"data: last" in stream.communicate(timeout=5)[0]
                assert stream.returncode == 0
            finally:
                if stream.poll() is None:
                    stream.terminate()
                    stream.wait(timeout=5)
        finally:
            release.set()
            proxy.terminate()
            proxy.wait(timeout=5)
            for server in (router, account):
                server.shutdown()
                server.server_close()


def test_session_owned_router_relays_account_requests_and_exits(tmp_path):
    import os
    import sys

    binary = Path(__file__).parents[1] / "target/debug/claude-codex"
    if not shutil.which("mitmdump") or not binary.exists():
        pytest.skip("build claude-codex and install mitmproxy for launcher integration")

    class AccountHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/oauth/usage"
            assert self.headers["Authorization"] == "Bearer toy-access"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ACCOUNT_RELAY_OK")

        def log_message(self, *_args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), AccountHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    source = tmp_path / "source"
    source.mkdir()
    (source / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "toy-access"}})
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        f"#!{sys.executable}\n"
        + """import os, subprocess
result = subprocess.run(['curl', '--silent', '--show-error', '--fail', '--max-time', '10', '--noproxy', '', '--proxy', os.environ['HTTPS_PROXY'], '--cacert', os.environ['NODE_EXTRA_CA_CERTS'], '-H', 'Authorization: Bearer toy-access', 'https://api.anthropic.com/api/oauth/usage'], check=True, capture_output=True)
assert result.stdout == b'ACCOUNT_RELAY_OK', result.stdout
print('ACCOUNT_RELAY_OK')
"""
    )
    fake_claude.chmod(0o700)
    state = tmp_path / "state"
    env = os.environ.copy()
    env["PATH"] = (
        str(fake_bin) + os.pathsep + str(binary.parent) + os.pathsep + env["PATH"]
    )
    env["CCP_ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{upstream.server_port}"
    env.pop("CCP_AUTH_TOKEN", None)
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "scripts/claude-account-router"),
                "--start-router",
                "--login-from",
                str(source),
                "--state-dir",
                str(state),
                "--",
                "--version",
            ],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "ACCOUNT_RELAY_OK" in result.stdout
        assert not list(state.glob("login-*"))
        match = re.search(
            r"Proxy listening on http://127\.0\.0\.1:(\d+)",
            next(state.glob("proxy-*.log")).read_text(),
        )
        assert match
        with socket.socket() as check:
            assert check.connect_ex(("127.0.0.1", int(match.group(1)))) != 0
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_main_account_selection_and_saved_defaults(tmp_path):
    saved = tmp_path / "accounts.json"
    saved.write_text(
        json.dumps({"main": "work", "accounts": {"personal": 1, "work": 2}})
    )
    accounts, main = launcher["account_selection"]([], saved)
    assert accounts == {"personal": 1, "work": 2}
    assert main == "work"
    accounts, main = launcher["account_selection"](["work=3"], saved, "personal")
    assert accounts["work"] == 3 and main == "personal"
    for entries, main in (
        (["../escape=1"], None),
        (["work=1"], "absent"),
        (["work=relative"], None),
        (["work"], None),
        (["work=1", "Work=2"], None),
    ):
        with pytest.raises(ValueError):
            launcher["account_selection"](entries, main=main)
    for content in ("{}", "[]", '{"accounts":{}}', '{"accounts":[]}'):
        saved.write_text(content)
        with pytest.raises(ValueError, match="nonempty accounts"):
            launcher["account_selection"]([], saved)


def test_account_login_source_preserves_keychain_path_spelling(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    alias = tmp_path / "profile-link"
    alias.symlink_to(source, target_is_directory=True)
    accounts, main = launcher["account_selection"]([f"work={alias}"])
    assert main == "work" and accounts["work"] == alias
    assert accounts["work"] != source


def test_multi_account_snapshots_keep_ownership_and_report_expired(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(launcher["sys"], "platform", "linux")
    sources = {}
    originals = {}
    for alias in ("work", "personal", "expired"):
        source = tmp_path / alias
        source.mkdir()
        (source / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"accountUuid": alias}})
        )
        credential = source / ".credentials.json"
        credential.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": alias,
                        "refreshToken": "never-copy",
                        "scopes": ["user:profile"],
                        "expiresAt": 1
                        if alias == "expired"
                        else time.time() * 1000 + 60000,
                    }
                }
            )
        )
        sources[alias] = source
        originals[alias] = credential.read_bytes()
    state = tmp_path / "state"
    state.mkdir()
    login = tmp_path / "login"
    login.mkdir()
    manifest = launcher["snapshot_accounts"](sources, "personal", state, login)
    assert manifest["main"] == "personal"
    assert manifest["accounts"]["personal"]["config_dir"] == str(state / "claude")
    assert "error" in manifest["accounts"]["expired"]
    for alias in ("work", "personal"):
        row = manifest["accounts"][alias]
        credentials = json.loads(
            (Path(row["secure_storage_dir"]) / ".credentials.json").read_text()
        )
        assert credentials["claudeAiOauth"]["accessToken"] == alias
        assert "refreshToken" not in credentials["claudeAiOauth"]
        assert (
            json.loads((Path(row["config_dir"]) / ".claude.json").read_text())[
                "oauthAccount"
            ]["accountUuid"]
            == alias
        )
    assert all(
        (sources[alias] / ".credentials.json").read_bytes() == original
        for alias, original in originals.items()
    )


def test_generated_mcp_retains_other_servers_and_native_chrome(tmp_path):
    manifest = {
        "main": "work",
        "accounts": {
            "work": {"config_dir": "/toy/config", "secure_storage_dir": "/toy/secure"}
        },
    }
    args = launcher["aggregate_mcp"](
        tmp_path,
        manifest,
        ["--chrome", "--model", "gpt-toy", "--mcp-config", "existing.json"],
    )
    assert args.count("--chrome") == 1 and "--no-chrome" not in args
    assert "existing.json" in args
    config = json.loads((tmp_path / "mcp.json").read_text())
    assert set(config["mcpServers"]) == {"account-chrome", "account-artifacts"}
    assert json.loads((tmp_path / "accounts.json").read_text()) == manifest
    assert (tmp_path / "accounts.json").stat().st_mode & 0o777 == 0o600
