# Remote Control owner, private sessions, HQ mirror passthrough; settings merge, trust union

Goal: HQ (cc-tmux-api) stays the primary Remote Control transport for `tcc`
sessions and ALSO mirrors each one to Anthropic's real Remote Control, so
claude.ai and the phone app see it. HQ's side is cc-tmux-api
`docs/devlog/claude_20261023-upstream-mirror.md`. This file is the launcher and
`--serve` proxy side.

## Placeholder grammar (single parser: `account_inject.parse`)

`proxy-account-<alias>[+rc=<owner>][+private]`, suffixes in any order. An
unknown suffix or two `+rc=` is a 401 at the proxy; a placeholder is never
forwarded.

- `+rc=<owner>`: on `/v1/code/*` only, the proxy injects `<owner>`'s token
  instead of `<alias>`'s, so one claude.ai login sees every account's sessions.
  Inference and everything else stay on `<alias>`. Set by
  `--remote-control-account`, defaulting to the accounts file's
  `"remote_control"` key; only the main alias's placeholder carries it.
- `+private`: changes no token. `account_inject` records it as
  `flow.metadata["account_private"]`, and `account_remote_route` adds
  `x-hq-private: 1` to the HQ registration (`POST /v1/code/sessions`). HQ
  stores the session as private and never mirrors it. Set by
  `--private-remote-control` or `TCC_PRIVATE=1` (`--via` only, main alias only).

### `+private` fails closed (review fixes, same day)

- Without `--via` the flag (or `TCC_PRIVATE=1`) is a `parser.error`. Only the
  `--serve` placeholder can carry `+private`; the local launcher would
  otherwise register with HQ unmarked and HQ would mirror it.
- `account_inject` drops the Authorization header (no token at all) on any
  `/v1/code/sessions*` request from a `+private` placeholder. The route addon
  never needs it (it sends HQ its own key), so a routed session is unaffected.
  When the route addon is not loaded, or does not claim the request (a create
  with no `bridge` key), Anthropic gets an unauthenticated request and refuses
  it, instead of the RC owner's token registering the session on claude.ai.
  Dropping the token, not answering 401 in `account_inject`, keeps this
  independent of addon order: inject runs before the route addon decides.
- Only `TCC_PRIVATE=1` means private, in the router and in the dotfiles `tcc`
  (`[ "${TCC_PRIVATE:-}" = 1 ]`). `client_environment` pops `TCC_PRIVATE`, so
  a `tcc` started inside a private session's shell is not private by
  inheritance. `--no-private-remote-control` overrides the variable.
- The folder-trust union reads siblings only in the
  `<state>/accounts/<alias>/claude/.claude.json` layout (before, a no-accounts
  or `--via` non-main profile globbed the parent of the state or temp dir), and
  skips a sibling that fails to read or parse: it belongs to another account's
  live CLI, which may be mid-write.

Why metadata and not the header: on `--serve`, `account_inject` loads before
`account_remote_route`, so the placeholder is already swapped by the time the
route addon sees the request. Metadata survives the swap whatever the addon
order. A client-sent `x-hq-private` is stripped; only the placeholder decides.

## Mirror passthrough

HQ's mirror talks to Anthropic through the same proxy with
`Authorization: Bearer proxy-account-<owner>` and `x-hq-mirror: 1`. The route
addon pops that header and does not redirect the request, so the mirror's own
`POST /v1/code/sessions` reaches Anthropic instead of looping back into HQ;
`account_inject` supplies the real token as usual. Worker calls use the
Anthropic-issued `worker_jwt`, not a placeholder, so they pass both addons
untouched. Any other value of `x-hq-mirror` is stripped and ignored.

## Launcher fixes from the same day

- `--settings` is no longer rejected. `take_settings` lifts every caller
  `--settings` (file or inline JSON) out of the claude args and merges it under
  the launcher's own settings; on an `env` key conflict the launcher wins
  (proxy, CA, base URL), and `apiKeyHelper` stays empty. Needed because HQ
  starts sessions with its own hooks file.
- Folder trust is the union of every sibling account profile
  (`<state>/accounts/*/claude/.claude.json`) plus the profile's own, so the
  trust dialog is answered once per repo, not once per account.

## Checks

`uv run --with mitmproxy==12.2.3 --with pytest pytest -q tests/account_router_test.py tests/account_inject_test.py tests/account_remote_route_test.py tests/account_chrome_route_test.py`:
63 passed, 2 skipped (50 + 2 before the private/mirror work, 57 + 2 before
the fail-closed review fixes). New tests
drive `main()` through `--via` with a fake proxy opener (flag, env and neither:
only the main placeholder gains `+private`), run inject then route on one flow
(private marking, forged header stripped, mirror passthrough keeps the
injected token), and pin suffix order independence and malformed-suffix 401s.
Each feature was mutated off in turn and each mutation failed at least one
test; the review fixes were mutated the same way (no `--via` guard, inject
keeping the token, sibling parse error, sibling glob scope, inherited
`TCC_PRIVATE`), each caught. Not exercised live: no `--serve` proxy or HQ was started for this change.
