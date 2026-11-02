# Cursor bridge reuse research

## Recommendation

Evaluate an existing standalone Messages broker before maintaining another Cursor protocol implementation. The strongest direct-interface candidate found is [D3Dream/cursor2api](https://github.com/D3Dream/cursor2api), pinned for evaluation at `ea652189aca4d3e244a126dffedd35116695d9a9`. Its documented interface matches Claude Code, and source inspection found native MCP registration and live tool-result feedback. This is a recommendation for an isolated test, not a reliability or permission-safety qualification.

Cursor remains excluded from automatic fallback. No hosted third-party broker receives account credentials. No protocol source has been copied or ported into this repository.

## Candidates checked

| Candidate | Interface and reuse fit | Verification and limits |
| --- | --- | --- |
| [D3Dream/cursor2api](https://github.com/D3Dream/cursor2api) | Standalone Go server with documented Anthropic Messages, Chat Completions, and Responses endpoints. [Pinned MIT license](https://raw.githubusercontent.com/D3Dream/cursor2api/ea652189aca4d3e244a126dffedd35116695d9a9/LICENSE) permits reuse with its notice retained. | Source has native MCP registration and retained-stream result feedback. A successful current Claude Code test was not established by the research. Automatic allowlist replies and web-tool approvals need explicit safety tests. |
| [ndraiman/pi-cursor-provider](https://github.com/ndraiman/pi-cursor-provider) | MIT Pi extension. Exports a loopback OpenAI Chat Completions proxy, not a documented standalone Anthropic server. | Version 0.1.11 failed this session's independent live test. Its schema did not decode a newer exec variant and the run stalled. Do not confuse native bridge architecture with current interoperability. |
| [ephraimduncan/opencode-cursor](https://github.com/ephraimduncan/opencode-cursor) | OpenCode integration with native tools and live continuation through an OpenAI interface. | Not a direct Messages integration. Source and issue review found result-fidelity and interrupted-loop risks. No current Claude Code acceptance was established. License must be checked before redistribution. |
| [offbynan/pi-cursor-provider](https://github.com/offbynan/pi-cursor-provider) | More recent Pi fork with a live echo-tool harness. | Author-reported tool replay covers Grok, not a proven Claude Code session. Advertised output budgets are not enforced provider limits. No independent run or license qualification was completed here. |
| [7836246/cursor2api](https://github.com/7836246/cursor2api) | Different project from D3Dream's server. | Archived on October 12, 2026. Not the maintained upstream to select for a new repair. |
| [h88782481/api2cursor](https://github.com/h88782481/api2cursor) | Cursor IDE calls third-party model APIs through this gateway. MIT. | Reverse direction. It does not supply Cursor-subscription inference to Claude Code. |
| [Cursor Bridge forum project](https://forum.cursor.com/t/cursor-bridge-2-0-use-cursor-in-web-beta-claude-code-or-codex/174457) | Hosted bridge discussed by its operator. | No verified self-hosted upstream or license was identified on the forum pages. Credentials traverse a third-party server, so it is excluded under this project's isolation constraints. Its operator reported recent Claude Code compatibility fixes, not independently verified here. |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | General subscription gateway with several documented providers and client integrations. | The primary README did not establish a Cursor-subscription upstream. Cursor client-configuration material is not proof of one. Some older documentation URLs returned HTTP 410. It is not qualified as a solution to this particular missing bridge. |

## Why the current bridge fails

The current Rust provider buffers upstream output, scans text for tool XML, and does not send the client's actual tool result back into a retained native stream. A fresh isolated Claude Code Read-only run returned a literal `toolu_…` identifier, executed no tool, and declared success after one turn. A custom advertised tool was described as unregistered by the model.

The Pi prototype has the right native-loop architecture, but its live test failed. The shipped schema did not recognize exec fields 36 and 55. Their meanings are not established by this observation. Retrying that package or trusting its README would not fix this concrete failure. Details are in `../devlog/claude_20261029-cursor-reuse-prototype.md`.

## Acceptance for any replacement

A candidate must demonstrate a real Read of a random-marker file, with an executable tool event and a marker-dependent final response. It must also preserve a custom tool's result, multiple sequential calls, error results, cancellation, and incomplete-stream errors. A literal tool identifier and a successful text response do not qualify.

Denied shell and write operations must remain denied, including native upstream paths. Unknown tool names must not execute. Session ownership and pending tool IDs must remain isolated. Context and output-limit claims must distinguish advertised adapter defaults from observed provider limits. Existing Cursor subscription credentials stay on the inference host and are never refreshed by diagnostics.

D3Dream's native execution and automatic approval paths are a gate before adoption. A documented standalone server is preferable to an undocumented deep import, but neither licensing nor interface compatibility proves that this gate passes.

## Evidence boundaries

The built-in deep-research workflow produced an adversarially checked shortlist. Its synthesis still lacked several requested classifications and exact licenses. Primary-source follow-up checked D3Dream's MIT text, the reverse-direction api2cursor README, CLIProxyAPI's provider description, and the forum project's hosting description. Claims not independently run remain labeled above.

Relevant pinned source includes D3Dream's [client](https://github.com/D3Dream/cursor2api/blob/ea652189aca4d3e244a126dffedd35116695d9a9/internal/cursor/client.go), [Messages adapter](https://github.com/D3Dream/cursor2api/blob/ea652189aca4d3e244a126dffedd35116695d9a9/src/anthropic.go), [live result feedback](https://github.com/D3Dream/cursor2api/blob/ea652189aca4d3e244a126dffedd35116695d9a9/src/live_run.go), and [native approval tests](https://github.com/D3Dream/cursor2api/blob/ea652189aca4d3e244a126dffedd35116695d9a9/internal/cursor/remote_tools_test.go). Private raw captures and workplace transcripts remain outside the repository. This report contains no production deployment or fallback reenablement authorization.

## Isolated D3Dream evaluation

The unmodified pinned broker subsequently passed ten HTTP tool-loop checks in each of agent and ask mode. Real Claude Code 2.1.309, running with a fresh HOME and only Read permitted, emitted an actual Read call and returned a random marker after two turns. This independently establishes the core client-tool round trip for the tested model, not general reliability.

Agent mode still forwarded native shell arguments with Cursor-only fields into Claude Code's Bash schema, causing input-validation errors. It also emitted a native Grep call not registered by the test client. Ask mode avoided that path but refused edits by its own read-only policy. Denied-operation canaries remained absent. The broker's local-exec helpers had no production caller, and no broker-host shell or file operation was observed.

This narrows the repair to the existing broker's tool boundary. A strict registered-MCP-only mode is being tested in an external upstream worktree. It is not yet qualified or integrated. No source protocol implementation is being copied into cc-proxy, no Cursor service is installed, and automatic fallback remains disabled. The exact tested upstream model was an effort-suffixed catalog ID rather than an assumed bare alias.
