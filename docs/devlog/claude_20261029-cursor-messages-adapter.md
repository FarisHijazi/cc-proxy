# Optional Cursor Messages adapter

## Ownership and behavior

Ecosystem research and live tests selected an existing standalone Messages broker rather than another hand-maintained Cursor protocol in Rust. The broker is D3Dream/cursor2api at `ea652189aca4d3e244a126dffedd35116695d9a9`, with a small opt-in registered-client-tool boundary patch in its own external Go worktree. Its native MCP transport and retained-stream result feedback remain upstream-owned.

The optional cc-proxy adapter uses `CCP_CURSOR_MESSAGES_BASE_URL` and `CCP_CURSOR_MESSAGES_API_KEY`. With the URL unset, the old path is unchanged. With it set, all Cursor Messages, token-counting, and compatibility-generation paths use the broker and never silently return to the broken native path. Exact `cursor:<catalog-id>` is required. Ask, plan, fast, and implicit default selection are refused rather than silently changing permissions or service tiers.

Only plain HTTP loopback origins are accepted, with an optional terminal `/v1`. Credentials, query strings, arbitrary paths, and malformed IPv6 suffixes are rejected. The adapter rebuilds minimal headers and changes the model to its exact provider ID. It never forwards a client's Claude bearer, cookies, account pool, or spoofed broker session header. The real session context becomes `X-Agent-Session-ID`.

HTTP and SSE stay in the existing Anthropic opaque relay. A separate direct-loopback constructor shares that implementation but bypasses ambient and explicitly configured outbound proxies. Normal Anthropic behavior is unchanged. Direct broker connections have a five-second connect limit and a 120-second idle-read limit. That read limit bounds silence between chunks, not total turn duration. No broker installer, process manager, service, or credential refresh was added.

## Review corrections

The first ambient-proxy regression accidentally inherited `NO_PROXY`, hiding a leak through `HTTP_PROXY` or `ALL_PROXY`. Independent review reproduced HTTP 502 rather than 200 with both NO_PROXY spellings unset. The environment guard now clears them and the direct relay bypasses all proxy configuration. Another regression covers malformed IPv6 tails. Generation explicitly forces `stream:true` even if raw passthrough contained false.

Nine adapter integration tests cover real Registry/server routing against a mock broker, tool-use and tool-result preservation, session identity, credential-header isolation, token counting, upstream statuses, a dropped stream, unavailable broker refusal, generation, and proxy bypass. Existing native Cursor tests pass with the option unset. Independent follow-up accepted the fixes, with longer-than-120-second silent thinking still an untested limit.

## Runtime and deployment boundary

The external broker's strict tool tests and a real editing task passed directly. Denied operations remained denied, but native questions caused a stall in the pinned upstream. A descriptor-verified question decline and explicit unknown-query errors were added to the same Go owner, plus native allowlist and send-error fixes found in review. Full upstream Go tests and vet passed, and independent review accepted the final source. Combined actual-client verification through this optional adapter passed on October 29. The evidence is below.

This optional Cursor adapter is local source only. It is not in the approved dashboard deployment. Cursor remains absent from both live fallback chains and source defaults. No broker service is installed and no production credentials were changed. The old native path remains broken when this option is unset. This tested alternative is not a production deployment.

## Combined actual-client acceptance

An isolated Claude Code process called the candidate cc-proxy, which relayed to the final strict broker on loopback. The account catalog model was `cursor:claude-opus-5-medium`. A fresh random marker made it impossible to pass by quoting the prompt.

- Editing completed with exit code 0 and five client turns. Actual client calls were Read, Edit, Edit, and Read. The output file exactly matched `FINAL` followed by the random marker. There was no schema validation error and no manual continue input. The client advertised Read and Edit, so Edit with an empty old_string created the file.
- Denial completed with exit code 0 in one client turn. Both write canaries stayed absent. Native operations received strict refusals, and no executable client tool call occurred. This run proves refusal completion, not a client-side Bash permission-error round trip. Earlier direct tests observed that permission error, but stalled before the question fix.
- The native-question decline is descriptor-checked and unit-tested. The final live denial did not establish that an AskQuestion interaction occurred, so the specific question path is not claimed as live-qualified.
- The harness stopped its captured broker and router children. Follow-up process and socket checks found no surviving test service. A token comparison over all 15 files in the new run found zero credential matches.

The private evidence is under the inference host's test directory `combined-final-20261029/run1`, with `report.json`, client transcripts, and broker/router logs. None of those transcripts are copied into the repository. The final broker binary has SHA-256 `1aa8587e9da9a72ebf17f3fa2a2909e5dd95dfed7decd8346599ff1fa82d7f04`.

This acceptance covers one current model and bounded tasks. Actual quota exhaustion, every catalog model, broker restart recovery, and more than 120 seconds without an SSE chunk are not live-qualified. The broker emits ten-second heartbeat chunks during normal operation. Pending work remains explicit rather than treating mock coverage as vendor-runtime proof.

## Final source verification

The bounded final `cargo test --locked -- --test-threads=1` run passed 1124 tests. `cargo fmt --check` and `cargo clippy --locked --all-targets -- -D warnings` passed. Ambient HTTP, HTTPS, and ALL proxy variables were cleared for the full suite. The adapter proxy-isolation regression independently sets hostile proxies and clears both NO_PROXY spellings, so this does not weaken its leak test. The first full-suite attempt inherited ambient proxies after clearing only NO_PROXY and stalled in an unrelated Codex mock listener. It was stopped and is not counted as a pass.

The live dashboard was rechecked at desktop and mobile widths after these source changes. It still serves the approved earlier binary, three default equivalence groups, four Codex family aliases, and Cursor-free fallback chains. All three production services remain active. No additional production restart occurred.
