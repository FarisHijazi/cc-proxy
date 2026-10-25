# Chrome fleet discovery and model error diagnosis

## Existing owner

`account_chrome.py` already merges live Chrome discovery across all configured Claude accounts. Native `/chrome` is a separate reserved, account-local UI. The installed CLI 2.1.310 has no `/chromes` alias and implements its native Chrome MCP server in-process. A child-command override cannot replace it. The documented constraint in `claude_20261018-multi-account-tools.md` still applies.

Added the requested `/chromes` command in the existing user-command repository, not a second proxy transport or browser registry. It calls the existing aggregate MCP tool and preserves account-prefixed IDs. Implementation and live command evidence are recorded in `~/.claude/docs/devlog/claude_20261024-cross-account-chromes.md`.

## Fleet observations and correction

Initial aggregate discovery returned three macOS profiles and one Linux browser across two available accounts. The native listing returned only two macOS profiles from the session's original account.

The development browser was stopped. Starting its existing profile from the tool environment inherited the inference proxy and caused browser TLS authority failures. Stopped only that owned launch and relaunched it with ordinary desktop networking, removing the proxy and process-scoped CA environment. Its extension then appeared in real aggregate discovery. The final observed count at this stage was three macOS and two Linux browsers.

Another VM uses a custom Chrome data directory. Its generated native-messaging host manifest existed under the standard Chrome root but was absent from the custom root. Linked the generated manifest into the custom root without hand-writing vendor-generated content. That desktop's X server is in uninterruptible sleep, and both the real-desktop viewer and display query could not complete. The browser is still not discoverable. No desktop restart, logout, machine reboot, or Docker change was performed. Two other expected hosts have Chrome binaries and native-host stubs but no standard browser profiles or running browser. They were not represented as connected.

Temporary desktop viewer ports bound only to loopback and were removed after inspection. The restored development browser remains running intentionally. No existing user Claude process was interrupted during Chrome inspection. A later, explicitly approved TTS recovery is recorded below.

## Separate selected-model failure

A reported GPT-6.1 model error was traced to a process without the account launcher's proxy, account, or CA environment. Its shared settings selected `gpt-6.1-sol[1m]`, so a direct Anthropic request returned `model_not_found` with HTTP 404. Other sessions routed through the deployed proxy continued receiving HTTP 200 for that model. This is a launch-path failure, not a missing model catalog entry.

The `cc()` shortcut in the dotfiles owner still called bare Claude. It now delegates to `claude-accounts`, with 12 passing Bash/Zsh argument tests and a successful real startup check. After explicit user approval, exited only the failed TTS process with native `/exit` and resumed the same conversation through the launcher. Its replacement environment includes the proxy, account profile, CA bundle, custom GPT model, and 1M context settings. Native Remote Control is active, and an actual GPT continuation produced a response and began tool calls for the original task. The original `cc` launch was visible in shell scrollback after exit. Recovery is recorded in `~/docs/devlog/claude_20261024-cc-account-routing.md`.

## Checks

The real `claude-accounts -- --model 'gpt-6.1-sol[1m]' --max-turns 6 --output-format json --print /chromes` invocation completed in four turns with `is_error=false` and no permission denials. It listed five actual connections, including all three macOS profiles, using the existing aggregate server. Whitespace checks passed. No commit or push was performed.
