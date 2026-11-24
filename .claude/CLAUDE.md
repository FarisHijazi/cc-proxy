# CLAUDE.md

Guidance for Claude Code and other AI tools working on this repository.

## What this is

A small local proxy that lets Claude Code talk to more than one backend at once,
chosen per request by the model name. Claude Code already speaks the Anthropic
Messages API, so the proxy speaks it too and forwards or translates each request:

- `claude-*` models go to Anthropic as a transparent passthrough that reuses
  Claude Code's own subscription login. No API key, no translation. The
  exception is `registry::REROUTED_MODELS` (`claude-opus-4-8` runs as
  `kimi-k3.1-pro`, owner's rule): `route_model` rewrites the id at entry, so
  routing, fallback tier and chain, and `x-claude-codex-served-by` all see the
  kimi model.
- `gpt-5.6-*` (and the other codex ids) go to the Codex backend using the
  ChatGPT subscription that the Codex CLI already logged in.
- `kimi-*`, `grok-*`, and `cursor*` ids go to their own translators.
- `glm-*` ids go to Z.ai's Anthropic-compatible API with the proxy's Z.ai key
  (`zai` backend, no translation; root `CLAUDE.md` trap 14).

The headline use is running the opus slot on a Claude subscription and the
sonnet slot on a ChatGPT/Codex subscription in the same session, switching
freely mid-conversation.

This is a fork of `raine/claude-code-proxy`. The dual Claude plus Codex routing,
the reasoning-across-switch handling, and the Codex-CLI credential model are the
changes made on top of it.

## Architecture

- `src/registry.rs` picks a provider for each request. `claude-*` and the opus
  and sonnet aliases resolve to the Anthropic passthrough; other ids match a
  backend exactly; an unknown id returns a 400 that lists the supported ids.
- `src/providers/anthropic/mod.rs` is the passthrough. It relays the original
  body and headers to `api.anthropic.com` and streams the reply back. It holds
  no Anthropic credentials.
- `src/providers/codex/*` translates the Anthropic Messages API to the OpenAI
  Responses API and back.
- `src/providers/translate_shared.rs` holds types and helpers shared by the
  translators, including synthetic reasoning provenance and input validation.

## Invariants to preserve

- The Anthropic passthrough stays byte-exact for native traffic. It drops unsigned
  `thinking` and proxy-signed `thinking` (Codex, Kimi, Gemini/local, Cursor CLI, agy, Z.ai),
  removing an assistant turn left empty. Genuine Anthropic signed and redacted
  thinking stays untouched. Only a changed body is reserialized.
- Private reasoning is never converted to visible assistant text. Cross-provider
  history keeps answers and supported tool calls/results, not another provider's
  thinking. Codex replays its own encrypted continuation; Kimi and local replay
  only their proxy-marked reasoning in `reasoning_content`. Gemini/local share
  the historical `ccp:gemini:v1:` marker. Cursor and agy paths need no reasoning replay.
  `translate_shared::ProxyReasoning` owns the unchanged persisted marker formats.
- Already-executed hosted web/X searches retain their complete public call/result
  records and text citations as deterministic text through `public_history_text`.
  They must not become client-executed function calls. Anthropic-native web search
  stays untouched; foreign X-search blocks are downgraded to public text there too.
- Unsupported documents, audio, video, unknown content, and unsupported images
  must fail explicitly at translation, before calling upstream. Codex tool-result
  documents use `translate_shared::document_to_text` only when lossless text is
  available. Otherwise `fallback::dispatch` tries only configured targets declaring
  `preserves_native_tool_documents`; the original document stays intact. Capability
  skips do not cool providers down, and exhausted capable targets still return 429.
  Gemini/local
  move tool-result images into the following user image parts; Cursor API takes
  base64 selected images, while Cursor CLI and agy take text only. Grok retains its
  explicitly configured image policy. Do not infer model-specific vision or tool
  support from a provider's request format. `tests/history_translation.rs` covers
  source histories across destination translators; it is not a live capability
  test of vendor models.
- Codex credentials come only from the Codex CLI's `~/.codex/auth.json`. The
  proxy has no Codex login of its own. Token refresh writes back to that file so
  the Codex CLI keeps working (OpenAI rotates the refresh token on use).

## Naming and distribution

The crate, the installed command, and the library target are all `claude-codex`
(`claude_codex` for the library, derived automatically from the package name).
The crates.io package is `claude-codex`. The GitHub repository is
`FarisHijazi/cc-proxy`; local checkout directories stay `claude-code-with-codex`.

Some strings deliberately keep the old `claude-code-proxy` name because they are
compatibility contracts, not the user-facing name. Do not rename them in a
future cleanup:

- The on-disk config and data directory and the macOS Keychain service, in
  `paths.rs`, `providers/kimi/auth`, and `providers/cursor/auth.rs`. Renaming
  these orphans any saved kimi, grok, or cursor login. `paths.rs` already has
  `legacy_config_dir` as the migration hook if this is ever changed on purpose.
- The Codex `ORIGINATOR` and `User-Agent` in `providers/codex`. These go to the
  ChatGPT backend, so keep them stable to avoid changing what the server sees.

Two install paths ship. crates.io via `cargo install claude-codex`, and prebuilt
binaries from the `v*`-tag release workflow in `.github/workflows/release.yml`.
That workflow uses the default `GITHUB_TOKEN` and needs no secrets or Homebrew
tap.

## Build and test

- `cargo build`
- `cargo test -- --test-threads=1`. Run tests single-threaded. A few config
  tests mutate process-wide environment variables and race under the default
  parallel runner. This is pre-existing and unrelated to product behavior.
- `just check` runs format, clippy, and tests together where the toolchain has
  clippy installed.

## Gotchas

- In the dual setup, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` must be
  unset. Claude Code forwards its subscription login for `claude-*` only when no
  explicit token is present. Setting either one sends that value instead and the
  Anthropic route returns 401.
- Claude Code's web search is a client-side `WebSearch` function tool. The
  hosted `web_search_20250305` tool only appears inside an isolated, history-free
  inner call that Claude Code makes to run the search, so its reconstructed
  `server_tool_use` and `web_search_tool_result` blocks never enter the outer
  transcript. The passthrough logs `hosted_web_search_in_history` if that ever
  changes, which would mean this assumption needs rechecking.

## Known limitation

Switching backends in the middle of an active tool call (for example pressing
Esc during a codex tool use, then switching to a Claude model and continuing)
can fail. Anthropic requires a leading signed `thinking` block in that position
and no valid signature can be produced for reasoning that came from another
backend. This is rare and not worked around.

## Style

Match the surrounding code. `reqwest` is built without gzip or brotli so bodies
are never auto-decompressed, and with rustls so no OS keychain is touched. Keep
new code in the same shape as the module it lives in.
