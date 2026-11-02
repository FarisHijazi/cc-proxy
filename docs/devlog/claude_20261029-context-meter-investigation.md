# Codex context-meter investigation

The user observed context percentage falling as a conversation grew. Read-only investigation compared recorded proxy response usage, the client transcript, native Claude Code 2.1.312 handling, and the configured statusline script.

## Confirmed

The native meter uses the most recent assistant response's input_tokens + cache_creation_input_tokens + cache_read_input_tokens divided by context-window size. Output tokens and cumulative token spending do not drive this percentage. Official reference: https://code.claude.com/docs/en/statusline#context-window-fields.

Codex `translate/live_stream.rs` sends a local estimated input count in message_start, then upstream-derived usage in the final message_delta. `translate/reducer.rs` correctly subtracts OpenAI cached tokens from inclusive input_tokens and reports that cached amount separately. A lower uncached input count alone is not shrinking context.

Observed examples in the investigated session, UTC:

- October 29 13:58:10: start estimate408402; final3014 fresh +394496 cached =397510.
- October 29 13:58:58: start estimate408901; final454 fresh +397568 cached =398022. Total context grew despite lower fresh input.
- October 29 11:01:12: Sol reported233138 total input; at11:51:40 after switching to Astra the report was224773, while message count increased123 to124. The reported decrease is verified; why upstream counted less is not established.
- Manual compaction on October28 genuinely reduced context. No October29 native compaction boundary was found in the inspected transcript.

The user's statusline rescales native remaining_percentage against an assumed usable window, normally reserving16.5%. This amplifies raw changes by about1.20 before rounding. Its code is external to this repository.

## Uncertainty and next check

Estimate-to-final correction is a plausible source of within-turn movement, but no statusline invocation capture proves both counts were rendered. Capture emitted statusline inputs correlated with request lifecycle before changing accounting. Never clamp the meter monotonically: that would hide real compaction/model-switch effects.

An empty Cursor fallback response reported one input token due to `cursor/sse.rs`'s missing-usage clamp. That response did not persist as an assistant transcript entry, so it is not proof of a visible reset. Cursor remains excluded from automatic fallback.

No source changes or credential operations were made for this investigation.
