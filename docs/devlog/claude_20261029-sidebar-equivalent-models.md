# Sidebar and equivalent models

## Delivered source

The dashboard now has Overview, Accounts, Routing, Models, Sessions, and Requests views. Its native hash navigation works with keyboard access and a collapsed mobile menu. Pico conditional CSS 2.1.1 and its MIT notice are bundled unchanged, so no frontend build, external stylesheet fetch, or extra service is required.

Models edits `fallback.tiers`, an ordered list of `{name, match, models}` groups. Exact normalized model matches precede case-insensitive family substring matches. Nonempty groups replace the built-ins. Missing or empty uses router defaults. The three destination columns cover Claude, Codex, and Cursor API. An absent destination skips that equivalent.

Rust `fallback::defaults()` owns the default chains and groups and publishes them in the existing `/v1/models` response. Python forwards that metadata without maintaining another list. Unknown defaults do not become an empty editor. `account_ui.py` remains the sole config writer and validates group bounds, duplicates, model IDs, and serialized size. Claude destination IDs must be full `claude-*` upstream IDs without client context hints. Codex aliases are valid and their catalog is prepared before fallback routing.

Cursor is removed from the source-code default chains as well as the previously changed live configuration. A Cursor destination does not enable a Cursor fallback step. No Cursor protocol repair is claimed by this change.

## Regressions caught before completion

- Switching provider to an unlisted configured destination selected “exact” but retained the previous model in the text input. The browser test failed with actual `claude-opus-5-5`, expected `sol[1m]`. Updating both fields fixes it.
- A malformed tier discarded otherwise valid custom chains and triggers. The new test failed with no chain instead of `accounts`. Tier deserialization now drops malformed entries separately.
- Cold custom Codex destinations were skipped before catalog discovery. The dispatch test returned 429 instead of 200. Awaiting discovery before target routing produces 200 and exactly one mock catalog GET.
- Equal defaults with different JSON property order were persisted as custom groups. Canonical comparison now resets to defaults regardless of object-key order. Serde JSON's ordered map representation was not itself a random-order bug.
- Native aliases could be saved as raw Claude upstream IDs even though this path does not perform Claude Code's expansion. The writer and input suggestions now enforce the supported full-ID contract.

## Verification on the integrated main tree

- Full Rust suite with an isolated HOME: 1,112 passed, zero failures.
- Strict all-target clippy with `-D warnings` and format checks passed.
- Full Python suite with project test dependencies and isolated HOME: 258 passed, one private-snapshot test skipped. Existing dependency deprecation warnings remain. The first run lacked the local cswap package and async pytest plugin; rerunning with those dependencies passed.
- Existing drag, provider, and session browser suites passed. Navigation tests cover desktop and 375px mobile layout, keyboard activation, loading and retry, unknown defaults, safe text rendering, draft and focus preservation, save rejection, semantic reset, and configured exact destinations.
- A new debug router process supplied its actual `/v1/models` response. The real stdlib dashboard, bound to an OS-assigned loopback port with a temporary accounts file, saved edits, preserved unrelated settings, reloaded them, and reset them. The router still listed Astra, Sol, Luna, and Terra aliases and no-Cursor default chains.
- Independent review accepted the integrated source after rechecking the fixes. The isolated HTTP fixture is ready only after binding and cleans up its captured child process.

Existing alias, launcher, and injector changes were preserved. The main source was not committed or pushed. The user explicitly approved deployment after verification, including brief proxy and dashboard restarts. No user sessions were restarted or given synthetic input.


## Approved production rollout

The locked release build passed. Its isolated real-app check confirmed the model aliases and default metadata, then exercised real dashboard save, reload, and reset against temporary configuration.

Installed only the release router, `account_ui.py`, `account_ui.html`, Pico conditional CSS, and its license. Exact SHA-256 guards checked both the staged candidates and the previous live files before installation. Backup and rollback scripts are under `~/.local/state/claude-codex/before-ui-equivalents-20261029T181148Z/` on the inference host. No config migration, credential change, launcher replacement, commit, or push occurred.

The deployed router SHA-256 is `7da90c1977e9ab4f01e05ab71d905111693d5552a37953974767340e7e45afc6`. The previous injector and launcher hashes remain unchanged. Only `claude-account-proxy.service` and `cc-proxy-ui.service` were restarted. HQ retained PID 209494 and its previous start timestamp.

Live Playwright checks at widths 1280 and 375 passed Models, Routing, Accounts, and Sessions navigation with zero page errors. The Models view showed three groups and the expected destination values. The CSS response matched the pinned file hash. All four Codex family aliases remained listed. Both active fallback chains and the router defaults exclude Cursor. The checks made no production configuration writes.

The first browser verification waited for a deliberately invisible checkbox input instead of its visible switch container. After correcting that test selector, all assertions passed. This was an observation error, not a production failure.

A later no-mutation production recheck at 375px initially asserted before the hash-change handler ran. The check now waits for `aria-expanded=false` before asserting the menu is hidden. The repeated production check passed at both widths with zero page errors. This was another test-observation timing error, not an application defect.
