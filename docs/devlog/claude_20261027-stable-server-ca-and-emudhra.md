# Stable server CA and eMudhra distrust

## Root cause and repair

The shared server inherited the launcher's account-specific state layout.
Changing its main account alias selected a different mitmproxy CA directory,
breaking clients whose `--via-ca` still trusted the original certificate.
The regression test in [account_router_test.py](../../tests/account_router_test.py)
failed for server mode and passed for client mode before the fix.

[claude-account-router](../../scripts/claude-account-router) now uses the
configured state root directly for `--serve`; client account isolation remains
unchanged. The [README](../../README.md#chaining-proxies---serve---via) records
the migration requirement. Before restarting the deployed account-proxy user
service, copied the original CA directory (including its existing signing key)
and preserved Remote Control session map into the stable state root. The
previous launcher and all old state directories remain available for rollback.
The active mitmdump process now uses `<state>/ca`, independent of account alias.

The dev VM's public CA fingerprint matched the original server CA exactly.
After the server repair, its unchanged launcher completed authenticated profile
bootstrap and printed Claude Code 2.1.311. The earlier laptop-only CA update was
reverted to the original trusted certificate. No client rollout is required for
clients retaining that original CA. TLS verification was never disabled.

## Separate eMudhra exposure

The private CA mismatch is independent of the public eMudhra compromise described
in [Cloudflare's advisory](https://blog.cloudflare.com/emudhra-certificate-compromise/).
However, an isolated copy of the deployed Rust router accepted the public
`dv-g3b-emudhra.we-have-a-problem.com` incident test chain (HTTP 200).
The cross-signed incident endpoint failed upstream. Ordinary Anthropic HTTPS
returned the expected HTTP 401 with toy credentials. No real login tokens were
sent to the incident endpoints.

[Cargo.lock](../../Cargo.lock) now pins `rustls-webpki` 0.103.16 and
`webpki-roots` 1.0.10, using the existing upstream fixes:
[compromised-key distrust](https://github.com/rustls/webpki/releases/tag/v%2F0.103.16)
and [affected-root removal](https://github.com/rustls/webpki-roots/releases/tag/v%2F1.0.10).
No custom TLS verifier was added. The replacement binary was built on the dev VM
from an isolated worktree at the same source commit as the original deployment,
with only these two locked dependency upgrades. The original dev release binary
and deployed server binary had identical SHA-256 hashes before rebuilding.

The replacement's actual Anthropic relay rejects both incident test endpoints
with HTTP 502 while ordinary Anthropic HTTPS still reaches authentication
(HTTP 401). The proxy's Python environment already has certifi 2026.10.18,
the version identified as patched by the
[certifi security advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2).
The original private CA has a valid matching RSA-2048 key; its directory and key
file permissions are 0700 and 0600. The key remains on the server.

An additional credential-free probe constructed the upstream TLS context using
the installed `mitmproxy.net.tls.create_proxy_server_context` function. It
successfully verified Anthropic and explicitly rejected both incident hosts
with OpenSSL certificate-verification errors. This checks mitmproxy's own
verifier rather than inferring safety from a separate browser or curl result.

Launcher suite on the dev VM with mitmproxy 12.2.3: 38 passed, two skipped
(the absent worktree debug binary and optional claude-swap installation).
The real forward-proxy routing and streaming tests passed. Python lint passes
with the two pre-existing findings FURB167 and TRY004 excluded; Rust format
checks and `git diff --check` pass.

Release validation: 1,067 Rust tests passed across 16 test groups;
`cargo clippy --locked --release --all-targets -- -D warnings` and
`cargo fmt --check` passed on the dev VM. Installed the verified release binary
on the proxy server, retaining `claude-codex-fork.before-20261027-emudhra` for
rollback, and restarted only the account-proxy user service. The installed
binary's SHA-256 is
`4239e7dd8b9424cc828b76614014906a00a3cd71d4697b30f29ac62c69e19c53`.
The dev launcher subsequently completed authenticated bootstrap over its
existing WebSocket fallback as well, using its unchanged CA file.
After startup settled, a real headless model turn from the dev VM connected
directly to the tailnet proxy and returned the requested `PROXY_TLS_OK` marker.

This checks the account proxy's trust paths, not the patch status of every
application, VM, container, public tunnel, or host trust store. System trust
stores and unrelated services were not modified.

The subsequent wider audit found additional tunnel/host exposure, including
disabled outer WSS verification in the launcher; that audit is kept with the
operator's private notes. The inner proxy TLS verification tested
here does not establish verification of that separate outer WSS connection.
