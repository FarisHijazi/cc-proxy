# `--serve` routes native Remote Control to HQ; launcher fixes

- `--serve --remote-control-backend URL --remote-control-key-file F` loads
  `account_remote_route.py` on the shared proxy, so every `--via` client started
  with `--remote-control` registers on that HQ. `--via` rejects the flags (the
  routing happens on the serving machine). Live on ftower, pointing at the hub.
- The routed-session map persists in `<state>/hq-remote-sessions.json` (0600).
  Without it a proxy restart forwarded live sessions' HQ worker tokens to
  Anthropic and broke them.
- `client_environment` sets `ANTHROPIC_CUSTOM_MODEL_OPTION` (default
  `gpt-5.6-sol`). Proxied sessions count as first-party, and Claude Code 2.1.30x
  then rejects non-catalog ids in Remote Control `set_model`; with it, HQ's model
  picker switched a live session to Codex and back.
- Launcher flags (`--chrome`, `--mcp-config`, `--remote-control`) go before a
  forwarded `--`; appended after it they became prompt text.
- `update_account_metadata` keeps folders trusted inside the profile when it
  re-mirrors `~/.claude.json`; trust used to be lost on every launch.

Checks: 47 router/addon tests (new: restart keeps routing, trust survives,
flag order). Live: real `tcc` sessions on dev reached the ftower hub natively;
the argv of a launched claude showed `--chrome --mcp-config … -- <prompt>`.
The HQ side and the captured claude.ai protocol are in cc-tmux-api
`docs/devlog/claude_20261023-native-ui-and-rc-protocol.md`.
