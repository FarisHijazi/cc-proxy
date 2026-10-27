# Automatic quota fallback across accounts, Codex and Cursor

## Problem

Two subscriptions ran out of quota while other accounts still had quota. Running
`cswap switch` did nothing for open sessions. `--via` clients never read the
client machine's cswap. Each session's account was fixed at launch by its
placeholder alias. One account existed only in the client's cswap, so the shared
proxy could not serve it at all.

## Change

- Moved the client-only cswap accounts to the proxy host with cswap's own
  `export --account` and `import`. Import skips accounts the host already has and
  leaves its active slot alone. The temporary export files were deleted. The proxy's
  private accounts file lists the new aliases in fallback order.
- `scripts/account_inject.py` is still the only credential owner. On
  `POST /v1/messages` from a placeholder client, it attaches every usable login, the
  session's alias first, as `x-claude-codex-account-pool`. It drops any
  client-supplied pool header, skips a slot whose token cannot be read for five
  minutes, and reloads the accounts file when its mtime changes.
- `src/fallback.rs` is used by the router, the routing owner. `dispatch_request`
  removes the pool header before capture or relay. The Anthropic relay also strips
  it as a second guard. Each request walks one chain per tier:
  - A Claude model tries each account, then Codex, then Cursor.
  - A Codex model tries Codex, then the Claude equivalent on each account, then Cursor.
  - A 429 moves on. A Claude account's 401 also moves on and quarantines the
    rejected credential for 60 seconds. Other failures are returned unchanged.
    No response is replayed after Claude Code has seen output.
  - Cooldowns belong to the registry, shared by requests through that router,
    rather than leaking between independent router instances.
  - A rejected (account, model) pair cools down until
    `anthropic-ratelimit-unified-reset` when the status is `rejected`. Otherwise it
    uses `retry-after`, or 60 seconds by default.
  - A thread delta never goes to an account that did not start the thread. The
    existing `thread_unsupported_request` refusal makes Claude Code resend the full
    history.
  - A Claude substitute for a Codex request rewrites `model` in the raw passthrough
    body.
  - The pool records the session's primary account explicitly. If that account's
    credential becomes unusable, the first surviving account cannot inherit its
    thread delta. Unknown ownership fails closed with `thread_unsupported_request`.
  - An empty Claude pool still reaches Codex and Cursor. The injector removes
    placeholder authorization instead of rejecting inference before routing.
- `retry::fail_fast` makes provider backoff report an exhausted budget for every
  attempt that has another option after it. Without this change, Codex's live-stream
  path retried its 429 up to 10 times. A streamed `gpt-6.1-sol` request hung past
  120 seconds before the fallback could run. The HTTP SSE reader captures this
  policy before `tokio::spawn` and scopes the child task explicitly, because Tokio
  task-local values do not propagate into spawned tasks. A mocked pre-output quota
  SSE regression failed with four upstream attempts before this fix and passes with
  one attempt afterward. Ordinary retry policy still makes four attempts.
- The Cursor backend now also borrows `cursor-agent`'s Linux login from
  `$XDG_CONFIG_HOME/cursor/auth.json`. It reads the file fresh each time and never
  copies it. Cursor's API wire IDs are `claude-opus-5`, `claude-fable-5` and
  `claude-sonnet-5`. The `-thinking-high` IDs from `cursor-agent --list-models`
  return `not_found`.

## Evidence

- An exhausted account returned HTTP 429 with
  `anthropic-ratelimit-unified-status: rejected` and a reset epoch, before any body.
  A healthy account returned 200 with `allowed_warning`.
- Exhausted Codex returned `429 rate_limit_error` through the router.
- Live checks after deployment:
  - An exhausted alias asking for Opus was served by the next account.
  - `gpt-6.1-sol`, `gpt-6-astra` and `gpt-6-luna` were served as Opus, Fable and
    Sonnet, both buffered and streamed.
  - A complete streamed probe on 2026-10-25 requested `claude-opus-5-5` and
    received `gpt-6.1-sol`, text `OK`, and terminal `message_stop` with HTTP 200.
    A separate direct streamed GPT probe also received terminal `message_stop`.
    After the spawned-SSE retry correction was deployed, another Opus request
    returned Sol, `OK`, terminal `message_stop`, and HTTP 200. Deployed binary
    SHA-256 is `e4e408492a9f567591e39d9eb478a109cc48e63b5831a75bddc9c32e7b31262e`.
    The injector's SHA-256 is
    `b17a9fa57c6a85221a7824db25dc9a82990012fac8147dd86944de6f89d899a8`.
  - Cursor answered for all three tier IDs.
  - The router log recorded more than 140 `quota fallback` events from real sessions.
- Four stalled client sessions were sent `continue` and resumed work. Two were on
  GPT models above 30% of a 1M context, and their requests were answered.
- Tests:
  - `tests/account_fallback.rs`: 12 integration tests, including account 401,
    empty Claude pools, and explicit thread ownership. Newly added regressions
    failed before their corresponding fixes.
  - `fallback::tests`: 6 unit tests.
  - Three Codex `http_stream_` tests, including the spawned quota SSE regression.
  - Account injection and launcher suites: 58 passed on 2026-10-25 using
    `uv run --offline --with pytest --with mitmproxy --with ../claude-swap python -m pytest tests/account_inject_test.py tests/account_router_test.py -q`.
  - The initial broader Python account-suite run had 96 passing tests.
  - Broader Python account and Chrome suites: 108 passed, one skipped.
  - Rust library suite: 875 passed with the known installed-`cursor-agent`
    missing-binary test excluded. The later server integration suite failed at
    `tests/server.rs:718`: unsupported image variations returned 502 instead of
    expected 404. This route has no pool header and does not enter quota fallback.
    The failure also reproduces in isolation. It also fails on an unmodified
    `HEAD` worktree with the same 502, so this change did not cause it. Cause:
    the account relay claimed `/v1/images/variations`, an OpenAI image route, and
    tried to forward it to Anthropic. `fallback_handler` now answers every
    `/v1/images/*` path locally with 404, so image payloads never reach Anthropic.
  - `missing_binary_produces_actionable_guidance` failed because `execvp`
    returns `EACCES` instead of `ENOENT` for a missing name when a `PATH`
    directory is unreadable. `cursor_cli::process::spawn` now rechecks with
    `binary_is_runnable` and reports the missing-binary guidance.
  - Fixed the `map_identity` lint in `src/providers/gemini/mod.rs`.
  - Final run with nothing skipped: every Rust test target passed (876 library
    tests), `cargo fmt --check` and `cargo clippy --all-targets -- -D warnings`
    passed. Deployed binary SHA-256
    `cc80eaa1b51eed6815cae6c5b3f93e9e2ddb9bdaedfb0bd793063ae579a9b497`. Live
    streams through it returned HTTP 200 with `message_stop` for Opus and Cursor.
  - The deployed router streamed `cursor:claude-opus-5`, the last tier in the
    chain, through the shared proxy: `OK`, terminal `message_stop`, HTTP 200.
  - Release build and formatting passed. Ordinary clippy passed with an unchanged
    `map_identity` warning in `src/providers/gemini/mod.rs:539`; strict
    `-D warnings` fails on that warning.

## Sticky load-balancing strategies (built 2026-10-25)

The accounts file's optional `balance` object picks each session's first account.
`account_inject.py` reloads it on the next request, so no restart is needed:

```json
"balance": {"strategy": "prefer", "prefer": "personal"}
```

- `session` (default): the placeholder's own alias, as before.
- `prefer`: the `prefer` alias first for every session. This is the proxy-wide main
  inference account and takes effect for running sessions too.
- `round-robin` and `least-sessions`: spread new sessions across accounts. The
  session keeps its account via `x-claude-code-session-id`, which subagents share,
  and is released after an hour idle, when its prompt cache has expired anyway.

No strategy rotates individual requests. A 429 or 401 still falls through the
rest of the pool and returns to the first account after its reset, costing one
cache rebuild. Five new tests cover each strategy, the idle release and config
validation. Deployed with `prefer: personal`. Fallback notes stopped right after
the switch, so that account was serving requests first-hand.

The design below was the original TODO. The served-account response header was
dropped: returning to the preferred account after its reset is the intended
behavior.

CURRENT (before): no request is ever round-robined. Each session starts on its own
placeholder alias. It moves to another account only on 429 or 401, and stays
there until the rejected account's reset passes.

PROPOSED: choose a session's starting account with a strategy, and keep every
request of that session on it.

- Key on `x-claude-code-session-id` (`src/request_identity.rs`), which subagent
  requests share with their parent session.
- `account_inject.py` keeps the sticky map `session id -> account`. It puts the
  assigned account first in the pool and marks it `primary`, so thread
  continuations stay with it.
- Strategies, all per new session:
  - `primary` (the current default): the session's own alias.
  - `round-robin`: the next account in the accounts file.
  - `least-sessions`: the account with the fewest live sessions.
- Failover must also stick. The router returns the account that served the
  request in a private response header. The injector updates the map and strips
  the header before the client sees it. Without this, a session would go back to
  its first account after each reset and lose its prompt cache.
- Expire entries after a session has been idle for a few hours.
- Tests: a session never changes accounts without a 429 or 401. A failed-over
  session stays on its new account. New sessions follow the strategy.

Spreading one person's sessions across several subscriptions may conflict with
provider terms. Keep `primary` as the default.

## Limits

- `/usage` still shows the session's own account, not the account that served the
  last request.
- Quota fallback uses 429. Claude-account 401 also advances the chain with a
  bounded credential quarantine. Other failures, including 5xx, remain client retries.
- `cswap switch` is not part of request routing. The proxy selects a usable account
  for each inference request. `claude-swap` remains the credential storage and
  refresh dependency; removing that dependency is a separate credential-owner
  migration, not a prerequisite for automatic per-request fallback.
- The proxy host and the client both hold refresh grants for the moved accounts.
  A refresh on one machine can invalidate the other's copy. Use those accounts
  through the proxy only.
