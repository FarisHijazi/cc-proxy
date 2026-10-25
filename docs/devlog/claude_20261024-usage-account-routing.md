# Usage follows inference, not Remote Control ownership

## Diagnosis

The user observed native `/usage` reporting an exhausted controller account.
Live requests to `/api/oauth/usage` with two existing placeholder aliases showed
one account at 100% weekly usage and another at 92%. Both retained their real
upstream percentages. Adding `+rc=<controller>` to the inference placeholder did
not change the usage account.

The routing implementation was already correct. `account_inject.resolve()` uses
the placeholder's base alias for inference and `/api/oauth/usage`; the `+rc=`
owner applies only to `/v1/code/`. Rust's account relay does not choose another
account or aggregate quota. The local launcher configuration still selected the
exhausted account as `main`.

## Resolution

Changed the private dev launcher account map to select the configured account
with remaining quota as `main` and preserve the original controller through the
existing `remote_control` setting. No new proxy write path, polling service,
quota cache, percentage rewrite, or production service restart was needed.
The machine-specific account map remains outside the repository.

Added `/api/oauth/usage` and its `at_wall=1&skip_spend=1` form to the existing
account-injection regression that proves Remote Control ownership cannot change
inference identity. README now explains native usage, launch-time selection,
Claude subagents, and the lack of automatic account failover.

## Live verification

- The normal dev `claude-accounts` shortcut selected the new inference default
  without explicit account flags. A real Sonnet request returned
  `ACTIVE_CLAUDE_ACCOUNT_OK`.
- Started an owned interactive TUI and opened native `/usage`. It displayed
  17% current-session usage, 92% weekly usage, and 46% weekly Fable usage for
  the selected inference account. No quota was fabricated or capped.
- The test session's self-hosted mirror reported connected. The Anthropic
  session listing authenticated as the original controller contained the test
  session with active status, independently verifying unchanged RC ownership.
- Closed the usage panel and exited only the owned test TUI using native
  `/exit`; no existing session was interrupted.
- Account-injection, launcher, Remote Control route, and Chrome route suites:
  66 passed, two dependency deprecation warnings. Ran with cached pytest,
  mitmproxy, and the local claude-swap dependency. An earlier run without
  claude-swap failed an integration test; rerunning with the dependency passed.

## Scope and limitations

This config change applies to newly launched dev sessions. Existing sessions keep
their launch account. Other client machines were not migrated. Native `/usage`
continues to describe the selected Claude account, even when the current model
is GPT. A live GPT request was already verified while the original Claude account
was exhausted, so the percentage alone is not evidence of a universal TUI gate.
Claude subagents still use their session's selected Claude account. Real upstream
quota errors remain enforced, and there is no automatic account switching.
