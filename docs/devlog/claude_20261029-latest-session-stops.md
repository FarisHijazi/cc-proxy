# Latest session stops

## Two distinct failures

The metrics research session used Cursor fallback at 09:54–09:56 UTC on October 29. That earlier failure is documented in `claude_20261029-overnight-fallback-forensics.md`. The latest stops did not use Cursor.

At 14:31–14:38 UTC, the client exhausted 13 retries. Twelve requests returned HTTP 502 with `WebSocket connect timeout after 15000ms`. One fallback to Claude returned HTTP 400 with an invalid thinking-signature error. The final client error occurred at 14:38:27 UTC. Its terminal envelope was a synthetic API error, not model task completion. The next user continuation arrived at 15:01 UTC.

The proxy recorded connection timeouts between 14:22:09 and 14:44:45 UTC across multiple clients. Some requests still succeeded early in that interval. A successful Codex reply followed at 14:45:04 UTC, before the approved 14:58 proxy restart. The evidence does not distinguish an upstream outage from the host's network path.

The signature error establishes that the Claude fallback rejected historical reasoning. It does not establish which provider originally created the rejected signature. The error's message index refers to the rewritten upstream request, not necessarily the stored inbound request. Do not fix this by indiscriminately removing every valid Claude signature.

## Later successful turns still stopped early

At 16:23:30 UTC, Astra returned HTTP 200 and `end_turn`, with text promising to continue and no tool calls. Its request still advertised 37 tools, had `max_tokens: 32000`, and no forced tool choice. The client then ran its stop hooks and returned to the prompt. Earlier turns around 16:06–16:07 executed Read and Edit successfully. This is not evidence of the Cursor translator or missing tool definitions.

The observed mechanism is a successful model final response rather than a connection failure. The model's reason for ending before completing the remaining work is not established. An automatic continuation policy would be a separate harness change, not a transport repair.

## Independent Cursor reproduction

A new isolated Claude Code 2.1.312 process used only the permitted Read tool against a random-marker fixture. With explicit `cursor:claude-opus-5`, it exited zero after one turn and reported a literal `toolu_…` string as its successful result. No tool-use event occurred and the marker was not returned. No input entered an existing user session.

A separate request advertised a custom echo tool. Cursor said that the tool was not registered, returned no executable tool call, and emitted `end_turn`. These live checks match the earlier incident independently of its historical traffic.

Source inspection found that the current Cursor API bridge buffers upstream output, parses XML from text, and discards tool results when resuming. It does not implement a genuine result-dependent native tool loop. Native MCP registration and live tool-result feedback require verification before declaring the translator repaired.

## Operational boundaries

The current live configuration was reread and excludes Cursor from both automatic chains. No client sessions or services were restarted during this diagnosis. No credential values were logged, refreshed, or distributed. Request bodies were restored with the traffic reader's existing deduplication owner before interpreting tool counts. Private traffic and workplace content remain outside tracked documentation.
