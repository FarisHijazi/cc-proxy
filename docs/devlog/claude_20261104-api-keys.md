# API keys for other apps

## What it does

Any Anthropic SDK or tool can use the account pool with a key the proxy issues. Set
`ANTHROPIC_BASE_URL=http://<proxy>:<api-port>` and `ANTHROPIC_API_KEY=ccp-...`. The request then
uses the accounts file's `main` account first, then the same pool and failover as a Claude Code
session.

## How

- `account_ui.py` owns the accounts file, so it issues keys. `{"api_keys": {"create": name}}` adds
  `{id, name, prefix, sha256, created}` and returns the plaintext once as `created_key`.
  `{"api_keys": {"revoke": id}}` removes the key. `/api/state` lists the keys without their hashes.
  The **API keys** view (`#keys`) wraps both.
- `account_inject.py` runs `api_key()` at the start of `requestheaders`. A `ccp-` key in `x-api-key`
  or `Authorization: Bearer` is hashed and compared to the stored hashes with `hmac.compare_digest`.
  A matching key is removed and replaced by `Bearer proxy-account-<main>`, and the request takes the
  normal placeholder path. An unknown key gets a local 401. Other keys pass through unchanged.
- `claude-account-router --serve --api-port N` adds `--mode regular --mode reverse:https://api.anthropic.com@N`
  to mitmdump. It reuses mitmproxy's reverse mode and needs no new listener code. In reverse mode, the
  existing `--map-remote` rule still sends model calls to the router.

## Checks

- Unit tests:
  - `tests/account_inject_test.py`: the key works in either header, an unknown key is refused, and `sk-ant-` keys pass through.
  - `tests/account_ui_test.py`: a key is created once and stored hashed, revoke works, and bad changes are refused.
  - `tests/account_ui_providers_test.mjs`: the real-DOM create, show-once and revoke flow.
- The 9 new Python tests failed before the change. The full Python suite (255) and the DOM suite pass.
- Wiring test: a real mitmdump in reverse mode with the real addon, in front of an echo router. A valid
  key reached `/v1/messages` with the key stripped and the pool header added. A wrong key got a 401, and
  an `sk-ant-` key passed through.
- Live on the shared proxy host:
  - Files are backed up in `~/.local/state/claude-codex/before-api-keys-20261104/`.
  - The `api-port.conf` drop-in adds `--api-port 18793`, and the UI drop-in adds `--api-base`. Both services were restarted.
  - A key created through the dashboard API answered a real `/v1/messages` call, served by the main account. A wrong key and no key both got a 401.
  - Regular sessions kept flowing after the restart.

## Known gap

The traffic recorder runs before the addon, so a key's rows have an empty `account` column. The
answering login (`served_by`) is still recorded.
