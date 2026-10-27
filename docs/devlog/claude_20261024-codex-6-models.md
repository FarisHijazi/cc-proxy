# Codex 6 model catalog and 1M launcher default

## Change

The installed Codex CLI 0.162.1 `model/list` returned four new ordinary selectable
IDs: `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, and `gpt-6-luna`.
All four are now accepted and listed, including their existing-style `-fast`
Priority-tier aliases. Hidden access-program models and the internal automatic
review model are not advertised.

`src/providers/codex/translate/model_allowlist.rs` is the only base catalog.
Its `selectable_models()` supplies both `Registry` and `CodexProvider`, removing
the duplicated list and duplicate fast-alias expansion. Existing alias targets
and GPT-5.6 Responses Lite policy are unchanged. GPT-6 uses the full lane.

The launcher defaults `ANTHROPIC_CUSTOM_MODEL_OPTION` to `gpt-6.1-sol[1m]` and
sets `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` in both the child environment and
launcher-owned settings. A caller's custom model option remains supported.
`[1m]` remains a Claude Code context hint and is removed before provider dispatch.
The context setting applies to new launcher processes; existing processes retain
their original environment and can select the explicit `[1m]` spelling.

## Verification

- Reproduced missing-model failures in the registry, Codex allowlist, server
  count-token endpoint, and launcher environment tests before implementation.
- Focused model tests and launcher environment tests pass after the change.
- Rust regression run with isolated system PATH: 1,047 passed, one image-route
  test explicitly excluded after reproducing its failure. The excluded test at
  `tests/server.rs:718` expected 404 for `/v1/images/variations` but received 502.
  It is outside the changed model flow; it was not fixed or claimed passing.
- The initial unit run failed the missing-Cursor-binary diagnostic test because
  the inherited PATH returned permission denied. An isolated system PATH fixed
  that test without a source change; all 869 library tests then passed.
- Launcher suite: 31 passed, four skipped. Broader Python addon collection could
  not run under system Python because `mitmproxy` was unavailable there.
- `cargo fmt --check` passes. `cargo clippy --all-targets` completes with an
  existing `map_identity` warning in `src/providers/gemini/mod.rs:539`; running
  with `-D warnings` fails on that unchanged warning.
- Independent reviewer found no confirmed correctness issues and reran the
  registry/model-allowlist tests and whitespace check successfully.

## Deployment and live evidence

Built the release binary and installed it on the existing shared proxy host,
with exact-path backups of the binary and launcher. Restarted only the account
proxy user service, which returned active. The deployed binary SHA-256 is
`25454ecb3de608d2a940a934e0bec560d58884b1179a5e094d81279e8f73af55`.
The deployed launcher SHA-256 is
`c109d91c464cd69ccaa7a2bb7652715cf964a75acef598d73928a39c3e5088da`.

Real Messages requests through the production account launcher returned:

| Selection | Result |
| --- | --- |
| `gpt-6.1-sol[1m]` | `PROXY_GPT_6_1_OK` |
| `gpt-6.1-sol` | `BARE_MODEL_OK` |
| `gpt-6.1-sol-fast[1m]` | `FAST_MODEL_OK` |
| `gpt-6-astra[1m]` | `ASTRA_OK` |
| `gpt-6-sol[1m]` | `SOL_OK` |
| `gpt-6-luna[1m]` | `LUNA_OK` |

The bare-model JSON result reported
`modelUsage["gpt-6.1-sol"].contextWindow = 1000000` and `is_error = false`.
This verifies client budgeting rather than merely checking environment strings.

The current live terminal session was switched to `gpt-6.1-sol[1m]` and its
status line confirmed the selection. Its structured Remote Control model change
was rejected because that already-running process still had the old single
custom option; the explicitly requested terminal `/model` command succeeded
through its native confirmation. No `/model` text was sent as a chat message.
