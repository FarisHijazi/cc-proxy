# Catalog and client-control capabilities

Captured three follow-up tasks in `.planning/todos/pending/`: dynamic account-independent catalogs, running-session inference-account switching, and measured evaluation of direct-client inference. UI/TUI styling is out of scope.

Current implementation:

- Most model catalogs are static. Cursor CLI already discovers models with a cache. Codex CLI model metadata on the investigated client includes supported IDs absent from the proxy allowlist. Discovery must feed both listing and validation.
- The proxy supports session_models but not session_accounts. Extend the existing account configuration writer and injector rather than adding a new listener or writer. A CLI and an in-session tool can share this control path without a local proxy.
- --serve --via already implements cascades. It forwards unknown account aliases upstream; it is not a credential-distribution service and does not eliminate the relay hop.
- Account changes must explicitly negotiate old thread ownership. A newly designated primary is not proof that an existing thread belongs to it.
- No latency benefit was measured. Distributing access tokens would require client authentication, account authorization, centralized refresh ownership, and a plan for usage reporting. A nominal lease cannot shorten the lifetime of the provider's bearer token.

Current official Claude Code docs provide a simpler picker integration than the old single custom-option setup: modelPicker can list multiple custom models. Gateway discovery uses /v1/models at startup, caches results, and accepts IDs containing claude or anthropic anywhere. Raw GPT/Gemini IDs still need explicit picker configuration. The repository comment describing starts-with filtering reflects behavior before v2.1.223. No user settings were changed in this investigation.

Sources:
- https://code.claude.com/docs/en/model-config#add-a-custom-model-option
- https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery

Gemini operational recovery: its existing service was running but looked in the old Chrome directory. Corrected the private GEMINI_CHROME_DIR configuration to the actual signed-in profile directory and retained Chrome cookie-refresh ownership. Restarted only Gemini, not the proxy or browser. Verified a real text response and a structured tool-call/tool-result round trip through cc-proxy. Media downloads were not tested. Details belong to the Gemini repository's `docs/devlog/claude_20261029-profile-path-recovery.md`.

No capability implementation, token distribution, commit, or push was performed. Gemini's configuration repair was the only runtime change in this investigation.
