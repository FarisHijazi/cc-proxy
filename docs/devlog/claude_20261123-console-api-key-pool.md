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

## Incident 2026-11-23 23:34 UTC: credit errors reached clients (fixed in the follow-up PR)

Measured from ftower's traffic DB, with the official prices fetched 2026-11-24:

- The key answered 183 messages from 23:33:15 to 23:42:20 UTC. That cost **$37.13**: Opus 5.5 $15.84,
  Sonnet 5.5 $14.27, Fable 5.2 $7.02. Nearly all of it was cache writes (2.77M 5-minute and 4.05M 1-hour
  tokens), because every session moved onto the key rebuilt its prompt cache there.
- The first 400 came at 23:34:54, after **$25.77** of proxy spend. 26 messages were refused, interleaved with
  129 more successes, so Anthropic's check is soft near zero.
- The refusal was `400 invalid_request_error` "Your credit balance is too low to access the Anthropic API…",
  sent with `content-encoding: br`. The router never decompresses, so the matcher could not read it and the
  400 went to Claude Code.
- The key cannot read Anthropic's cost or usage reports: 401 "The Admin API requires an Admin API key or an
  organization-scoped API key".
- Guess, unverified: only about $26 of credit was left when the proxy started. Whether the on-call's direct
  use spent the rest, or the balance was never $200, is visible only in the Console.

Fix: key attempts ask for `accept-encoding: identity`, `billing_error` counts at any status, and an
unreadable key 400/402 falls back as well. The proxy also enforces `monthly_budget_usd` itself, before
each request (see CLAUDE.md).
