# Console API key as the preferred pool member (2026-11-23)

The owner decided that one Anthropic Console API key is tried before the Claude subscriptions,
then Codex. The key's workspace has a monthly spend limit, and that limit is the hard stop.

## Configuration (ftower `--serve` accounts file only)

```json
"api_key_accounts": {"console-api-key": {"key": "<Console key>", "preferred": true}}
```

The old router ignores this section, so a rollback simply returns to subscriptions first.
`balance.prefer` must still name a subscription: older code refuses an unknown alias there, and
`balance` keeps ordering the subscriptions after the key.

## Measured with the real key (isolated router on ftower port 18997, temp HOME)

Each request was a recorded Claude Code request (headers plus `context_management`, `diagnostics`,
`safeguards`, `data_flow`, `thinking`, `output_config`, three tools, a trimmed system prompt), sent as
`claude-haiku-5-5` with `x-api-key` and no `Authorization`:

| Probe | Result |
| --- | --- |
| All 17 recorded betas, `oauth-2025-04-20` kept | 200 |
| Same, `oauth-2025-04-20` removed | 200 |
| `thread: create`, then `thread: continue` on its id (`message-threads-2026-08-12`) | 200, 200 |
| `claude-sonnet-5-5` with `context-1m-2025-08-07` | 200 |

So nothing needs stripping beyond swapping `Authorization` for `x-api-key`, and thread deltas
work on the key. Its rate-limit headers report 13,000 requests/min.

## Not measured

The credit and spend-limit error text was not captured, because that would mean exhausting the
key. The match uses the documented forms: a 400 `invalid_request_error` whose message contains
"credit balance is too low" or "reached your specified ... API usage limits", or any 402
`billing_error`. Any other 400 from the key is returned unchanged and never retried.
