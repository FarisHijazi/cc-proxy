# cc-proxy dashboard

`scripts/account_ui.py` (packaged as `cc-proxy-ui`) serves `scripts/account_ui.html`, one page
with no build step or dependencies.

## What it shows and changes

- Accounts in fallback order (the accounts file's key order), each with live usage windows
  (`/api/oauth/usage`) and its email and plan (`/api/oauth/profile`). Both are fetched through the
  proxy with `proxy-account-<alias>` and cached for 60 s, so `account_inject.py` stays the only
  owner of tokens.
- Settings it writes: `order`, `balance` (strategy, prefer), `disabled` and `traffic_days`. Each
  write is validated and atomic, and it keeps `accounts.json.before-ui`. Other keys are untouched.
- `disabled` is new in `account_inject.py`. `pool()` leaves those accounts out of fallback, the
  spreading strategies skip them, and a disabled `prefer` falls back to the session's own alias.
- Activity: model calls per answering account, machine (`client_host`, else `client_ip`) and model
  over 1 hour, 24 hours or 7 days, plus the request table and the full request and reply.

## Deployed

On ftower as the user unit `cc-proxy-ui.service` on `100.103.162.14:18792`. Port 18791 is held by
another process (a second `mitmdump`, not this work). Verified against live data: a settings
save rewrote the file with a backup, a bad `order` was refused, and model calls kept returning 200
after the save. Screenshots were checked in light and dark with headless Chromium.

## Found while building it: setup-tokens were never used

Three of the five slots hold `claude setup-token` credentials: an access token and
`["user:inference"]`, with no `expiresAt` and no refresh token. `slot_token()` treated the missing
expiry as expired, tried to refresh, failed, and raised. The proxy then answered those aliases
with its own 401 and `pool()` left them out, so they never served a request. `slot_token()` now
uses such a credential as-is and re-reads it hourly. Verified: a request pinned to each of the
three (`+pin`) was served by that account.

A setup-token cannot call `/api/oauth/usage` or `/api/oauth/profile`. The dashboard marks these
accounts `setup-token` and shows usage from the latest reply's `anthropic-ratelimit-unified-5h-*`
and `-7d-*` headers, which the traffic database records. This also means a client whose `main` is a
setup-token account cannot launch, because the launcher needs that profile.
