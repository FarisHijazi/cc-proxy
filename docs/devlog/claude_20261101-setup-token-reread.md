# Replaced setup-tokens took up to an hour to apply

A revoked `claude setup-token` was replaced with `cswap add-token - --email <account>`, which updates the slot in place. The proxy kept serving the old token from its in-memory cache, which held setup tokens for 60 minutes. Upstream rejected that token with 401, so the auth fallback moved every request to the next account, and `balance.prefer` looked ignored.

`SETUP_TOKEN_REREAD_MS` is now one minute. A setup token has no expiry and no refresh grant, so rereading it is only a local cswap file read. `tests/account_inject_test.py::test_setup_tokens_are_used_as_is_and_never_refreshed` failed with a one-hour horizon and passes now. All 38 injector tests pass.

Production was updated with a hash-guarded atomic file replacement and a backup under `~/.local/state/claude-codex/before-setup-token-reread-20261101/`. mitmproxy reloaded the `--scripts` addon without a service restart. The proxy kept its main PID, and the preferred account served its first request about 30 seconds later, with HTTP 200.
