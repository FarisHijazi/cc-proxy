# Claude model ids from Anthropic, not a fixed list

## Problem

The router listed Claude models from `ANTHROPIC_STYLE_ALIASES`, a fixed list in `src/registry.rs`. After a model release
it was out of date: the dashboard showed 12 Claude ids and was missing `claude-opus-5-5`, `claude-sonnet-5-5`,
`claude-haiku-5-5` and `claude-fable-5-1`. Routing was not affected, because any `claude-*` id goes to Anthropic.
The same list also decides which names move when the alias provider is switched, so making it longer was the wrong fix.

## Change

- `providers::anthropic` keeps the Claude ids from Anthropic's last `/v1/models` reply (`live_models`). The list is fresh
  for 10 minutes. A reply without ids keeps the previous list.
- `handler_models` refreshes that list when the call carries an `Authorization` header and the list is stale. It uses
  the passthrough's own `handle_account_request`, so it uses the same upstream proxy. It adds `anthropic-version` and the
  OAuth beta flag when the caller sent neither. The router still holds no login.
- `Registry::supported_models_for("anthropic")` lists the four family aliases plus the live ids once they are known.
  Otherwise it uses the fixed list as before.
- `account_inject.py` lends the first enabled account's login to a token-less `GET api.anthropic.com/v1/models`, which is
  how the dashboard and the launcher ask. It does nothing when a further proxy holds the logins.

## Checks

`tests/live_claude_models.rs` checks four things. Without a login the router lists the fixed list and makes no upstream call. With a login
it lists the live ids, sends the token and the OAuth beta, and drops ids Anthropic no longer lists. Callers without a login
then see the cached list. A second call within the TTL does not refetch. With the refresh call removed, the test fails at
"live list once a login asked". Two addon tests cover the lent login and the cascading-proxy case. `cargo test -- --test-threads=1` passes all
tests. The Python suite gives 246 passed.

A direct probe with one account's unexpired token (read without refresh) returned 14 Claude ids from Anthropic.

## Deploy note

The live router binary on the proxy host was built from a scratch copy that is not in git. That copy is an older base plus the
`local` backend. The change was applied to a copy of that tree and built from there, so the local backend stays.
That backend still needs to land in git.
