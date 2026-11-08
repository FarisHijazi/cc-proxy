# API-key usage attribution

`account_traffic.py` remains the only traffic persistence owner. It snapshots the authorized key's configured name from flow metadata into a nullable `api_key` column when recording completes, without storing the presented credential. Existing databases gain the TEXT column additively. Calls without attribution remain grouped as `Unknown / pre-key` in Usage, separate from named keys and from an empty result.

The Usage API aggregates this field alongside its existing call and token buckets. The dashboard displays the key buckets. Coverage includes name attribution, connection-error attribution, legacy schema migration, and separation of named-key calls from un-attributed calls. DOM coverage checks escaped names, unknown tokens, and the loading state.

After approval, deployed `account_traffic.py` and the dashboard to the shared proxy and restarted the proxy and dashboard services. A live call with the existing default key returned `usage verified`; `/api/usage?hours=1` reported its name with one call, 11 input tokens and 6 output tokens. Isolated Chromium opened the real deployed `#usage` view and verified that row, with no horizontal overflow. Focused Python tests passed 115 tests and the dashboard DOM suite passed.
