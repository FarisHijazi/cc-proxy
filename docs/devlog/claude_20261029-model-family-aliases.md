# Latest-family aliases and default context hints

## Behavior and ownership

Codex families `astra`, `sol`, `luna`, and `terra` resolve the highest numeric version in the provider-backed catalog. Explicit IDs remain pins. `-fast` retains Priority-tier behavior. Native `opus`, `sonnet`, `haiku`, and `fable` retain their provider routing and Claude Code's native version resolution.

The existing Codex model owner now supplies aliases, listing/routing, native Responses, Chat Completions, and validation. `model_catalog.rs` reuses the provider's model-list endpoint with a 300-second on-demand TTL, three-second timeout, bounded body, and no redirects. Discovery reads existing access credentials without refreshing or writing them. Compatibility baseline IDs do not require discovery. Hidden/non-API discovered rows are excluded.

Async endpoints await blocking-worker discovery without blocking Tokio. Cache generation invalidates on credential replacement. Failed discovery can retain only a same-generation snapshot; unverifiable identity-bearing CLI caches are skipped rather than assigned to another account. The compatibility baseline remains available.

Launcher picker entries use stable family `[1m]` selections without `behavesAs`. Custom rows are preserved. Explicit launch `--model` options receive the default suffix unless one is already present. Arguments after `--` remain opaque. Existing `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000` gives bare custom aliases the requested client budget.

`[1m]` is a client context hint, not proof of upstream capacity. Current provider metadata reports smaller windows for some GPT models. Native-client mock tests establish the requested budgeting behavior, not million-token upstream acceptance.

## Verification

- Baseline real binary rejected `astra[1m]` with HTTP400 and unknown-model Astra. Explicit-version controls passed.
- Final release socket-level tests route all four aliases to concrete fixture models. An isolated real Claude Code2.1.312 client using bare Astra reports contextWindow1000000 and consumes the response successfully.
- Independent native-client tests cover bare Opus, Sonnet, Haiku, Fable, GPT Astra, and Gemini Flash with one-million-token client budgeting.
- Future-version fixtures verify numeric6.10>6.9 and newly catalogued Astra versions without claiming those future IDs exist today.
- Independent review caught discovery blocking, cross-account stale-cache retention, prompt mutation after `--`, and effective-provider override refresh. All four were corrected with targeted regressions and re-reviewed before deployment.
- Final isolated Rust suite:1104 passed,0failed. Formatting and strict all-target Clippy passed.
- Feature-worktree Python suite:231 passed,1 optional live-Chrome skip. Combined main with the earlier active-token fix:233 passed,1skip. Existing dependency deprecation warnings remain.
- Three existing Ruff findings in the launcher were reproduced on HEAD and not changed by this feature.

## Approved rollout

The user explicitly approved deployment after verification and a brief proxy-only restart. Installed release SHA-256 `2f7414e8714694f893529509bb53cd06b64d21fa5ba6ef02060232af22ba7a43` and launcher SHA-256 `663e0f5cc4aa58a802f542048469f0479cbb0f2fdfee898a4d4b63cb049e029f` on the shared proxy. Previous binary and launcher are backed up under `~/.local/state/claude-codex/before-family-aliases-20261029/`, alongside an exact-target rollback script.

The earlier active-token addon remains unchanged at SHA-256 `6a02b8ab284b2f3e010b7f4425df8ebdd60419d797b974fe258899b547c5950c`. Proxy, dashboard and HQ all remain active. Cursor remains excluded from automatic fallback. No client sessions were restarted or given synthetic input.

Production checks returned HTTP200 and the exact requested marker for each of `astra[1m]`, `sol[1m]`, `luna[1m]`, and `terra[1m]`. Live catalog models are respectively `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-luna`, and `gpt-6.1-terra`. Wire replies retain the family alias label; provider request logs carry concrete model IDs.

Updated the existing launchers on four consumer machines. Used a narrow patch to preserve host-specific TLS/browser differences. Two deployed directories were not Git repositories themselves: git apply from those paths silently changed zero files, caught by post-install behavior assertions. Direct scoped patch application corrected the rollout; all four then passed checks for default suffix, eight family picker rows, and opaque positional arguments. No settings/credentials were rewritten. New picker/environment defaults require new launcher processes; running sessions can explicitly select family[1m].

No commit or push was requested/performed. Sources and tests are integrated in main's working tree; the implementation worktree remains for audit.

## References

- https://github.com/openai/codex/blob/main/codex-rs/models-manager/src/manager.rs
- https://code.claude.com/docs/en/model-config#add-a-custom-model-option
- https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery
