# Personal full login: computer use and cross-account Chrome, live

Continues [native Chrome account](claude_20261018-native-chrome-account.md) and
HQ's computer-use record (`cc-tmux-api/docs/devlog/claude_20261018-native-computer-use.md`),
both of which stopped because the only Pro/Max account had an inference-only
setup-token (cswap slot 1: `scopes=[user:inference]`, no refresh grant).

## Account storage: claude-swap owns it

- Full claude.ai login for the personal (Max) account in a scratch directory
  (`CLAUDE_CONFIG_DIR=<dir> claude auth login --claudeai`), then stored into
  its existing cswap slot with `CLAUDE_CONFIG_DIR=<dir> cswap add`, which
  replaced the setup-token in place. (`cswap export` was taken first.) The fork
  fix `4674e69` keeps that add from marking slot 1 active while `~/.claude`
  holds the work login.
- The scratch directory's credential was then deleted, not logged out, so cswap
  is the **only** holder of the rotating refresh grant.
- The account map is unchanged: `{"main":"work","accounts":{"work":2,"personal":1}}`.
- `bootstrap_cswap` still snapshots access-only. When a slot's access token has
  expired, it now calls cswap's own `consume_backup_grant` (locked, CAS-persisted)
  and re-reads the slot once. It refuses when the slot is the live default
  login, because that grant is shared with `~/.claude` and only claude refreshes
  it. Live check: forcing the expired path on slot 1 moved its expiry from 7.3h
  to 8.0h, the snapshot carried no refresh token, and `cswap list` still showed usage.
- To add another full login, use the same two commands, then delete the scratch
  directory's credential.

`tests/account_router_test.py` passes 17. Its live
`test_session_owned_router_relays_account_requests_and_exits` fails with or
without this change: curl exit 56 through mitmproxy to the real API.

## Computer use

`computer-use` is gated in 2.1.302 by plan (`max`/`pro`) **and** the GrowthBook
flag `tengu_malort_pedway.enabled`, read from the config's cached features. A
config directory's first launch has no cache, so the server is missing from
`/mcp` until the **second** launch. This happened both natively and through the
launcher, so it is not a proxy bug.

Live, through `claude-accounts --main-account personal` with HQ Remote Control
and Sonnet 5.5: `/mcp` listed `computer-use`, enabling it worked, and
`request_access` for Calculator raised the native dialog. Denying it returned
empty grants to the model. No screenshot or input action was run.

The dialog appeared **only in the terminal**, never in HQ. The REPL forwards a
`request_user_dialog` over Remote Control only when `i_n(kind,"remote_control")`
allows it, and in 2.1.302 that covers just `refusal_fallback_prompt` and
`fable_overage_consent_prompt`. HQ's `computer_use_approval` declaration is
accepted but never used by an interactive CLI, so approvals must be answered in
the terminal.

`gpt-5.6-sol` hung on "No response from the API" during this test. Both the
private router and `:18765` returned `rate_limit_error` from the Codex backend,
so the cause is upstream rate limiting, not the relay.

## Native Chrome, work main → personal

`claude-accounts --main-account work --chrome-account personal`: `/chrome`
showed Enabled, Installed, and three browsers (2 macOS + 1 Linux).
`list_connected_browsers` returned the personal account's device IDs, which differ
from the two the work account sees. This is the real two-account redirect that
the earlier record could only show with a synthetic source.
