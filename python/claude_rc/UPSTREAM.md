# Vendored: claude-rc-api

Copied from https://github.com/ThatCrispyToast/claude-rc-api at `0c4f384`
(0.3.0, "the Claude Code 2.1.301 controller surface"), MIT licensed — see
`LICENSE` in this directory. The fork is `FarisHijazi/claude-rc-api`.

`API_REFERENCE.md` is upstream's protocol write-up. Local changes start after
this commit; `git log -- python/claude_rc` lists them.

## Local changes (0.4.0)

Version bumped to 0.4.0 locally to mark a surface upstream does not have. It
covers Claude Code on the web, not only Remote Control:

- `client.py`: one HTTP path (`_send`), web header set (`build_headers`),
  §9 retries, `APIError.error_type`/`.resource`, `mark_read` `{event_id}`.
- `credentials.py`: `resolve_auth` (explicit, env `CLAUDE_AI_ACCESS_TOKEN`,
  sessionKey cookie, cswap slot, `~/.claude`), lazy org lookup.
- New mixins: `lifecycle.py` (create/query/tag/unarchive/delete/share,
  teleport events), `environments.py`, `triggers.py` (scheduled tasks),
  `interaction.py` (every control subtype, permission and AskUserQuestion
  answers, slash commands, stream meta frames, account watch feed),
  `files.py` (attachments, Files API), `tts.py` (claude.ai TTS websocket
  and an Ogg Opus muxer).
- `cli.py`: subcommands for the whole surface; `webui.py`: create session
  and answer questions.

Shapes and what still needs a live check:
`docs/devlog/claude_20261028-claude-code-web-api.md` in the cc-proxy repo.
