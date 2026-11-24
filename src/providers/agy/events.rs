//! `agy --output-format stream-json` -> Anthropic Messages SSE.
//!
//! Shapes captured from agy 1.5.16 (fixtures in `tests/fixtures/agy/`):
//!
//! ```text
//! {"event":"init","conversation_id":"…","init":{"cwd":"…","tools":[…],"permission_mode":"…"}}
//! {"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"read_file","tool_info":{…}}}
//! {"event":"step_update","step_update":{"step_index":4,"state":"ACTIVE","step_type":"agent_response","text_delta":"…"}}
//! {"event":"result","result":{"status":"SUCCESS","response":"…","usage":{…},"denied_actions":[…]}}
//! ```
//!
//! `text_delta`s are true deltas (no settled repeat, unlike cursor-agent), and
//! `result.response` is their concatenation, so it is only a fallback for a run
//! that streamed no text. Tool steps are agy's own, already executed: they are
//! surfaced as thinking, never as `tool_use`.

use serde_json::{Value, json};

use crate::anthropic::sse::encode_sse_event;
use crate::providers::cursor_cli::AgentTranslator;
use crate::providers::translate_shared::ProxyReasoning;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum OpenBlock {
    None,
    Thinking(usize),
    Text(usize),
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RunUsage {
    pub input_tokens: u64,
    pub cache_read_tokens: u64,
    pub output_tokens: u64,
}

pub struct EventTranslator {
    message_id: String,
    model: String,
    line_buffer: Vec<u8>,
    message_started: bool,
    open: OpenBlock,
    next_index: usize,
    emitted_text: String,
    /// The `step_index` the last text delta belonged to, so a later answer step
    /// starts on a new paragraph.
    text_step: Option<u64>,
    usage: RunUsage,
    finished: bool,
}

impl EventTranslator {
    pub fn new(message_id: impl Into<String>, model: impl Into<String>) -> Self {
        Self {
            message_id: message_id.into(),
            model: model.into(),
            line_buffer: Vec::new(),
            message_started: false,
            open: OpenBlock::None,
            next_index: 0,
            emitted_text: String::new(),
            text_step: None,
            usage: RunUsage::default(),
            finished: false,
        }
    }

    pub fn usage(&self) -> &RunUsage {
        &self.usage
    }

    pub fn is_finished(&self) -> bool {
        self.finished
    }

    pub fn push_bytes(&mut self, chunk: &[u8]) -> Vec<u8> {
        self.line_buffer.extend_from_slice(chunk);
        let mut out = Vec::new();
        while let Some(position) = self.line_buffer.iter().position(|byte| *byte == b'\n') {
            let line: Vec<u8> = self.line_buffer.drain(..=position).collect();
            let line = String::from_utf8_lossy(&line);
            if let Ok(event) = serde_json::from_str::<Value>(line.trim()) {
                self.push_event(&event, &mut out);
            }
        }
        out
    }

    fn push_event(&mut self, event: &Value, out: &mut Vec<u8>) {
        if self.finished {
            return;
        }
        match event.get("event").and_then(Value::as_str) {
            Some("step_update") => {
                if let Some(step) = event.get("step_update") {
                    self.on_step(step, out);
                }
            }
            Some("result") => {
                if let Some(result) = event.get("result") {
                    self.on_result(result, out);
                }
            }
            _ => {}
        }
    }

    fn on_step(&mut self, step: &Value, out: &mut Vec<u8>) {
        let state = step.get("state").and_then(Value::as_str).unwrap_or("");
        match step.get("step_type").and_then(Value::as_str) {
            Some("agent_response") => {
                let Some(delta) = step.get("text_delta").and_then(Value::as_str) else {
                    return;
                };
                if delta.is_empty() {
                    return;
                }
                let index = step.get("step_index").and_then(Value::as_u64);
                if self.text_step != index
                    && !self.emitted_text.is_empty()
                    && !self.emitted_text.ends_with("\n\n")
                {
                    let gap = if self.emitted_text.ends_with('\n') {
                        "\n"
                    } else {
                        "\n\n"
                    };
                    self.text(gap, out);
                }
                self.text_step = index;
                self.text(delta, out);
            }
            Some("tool") if state == "ACTIVE" || state == "ERROR" => {
                let name = step
                    .get("tool_name")
                    .and_then(Value::as_str)
                    .unwrap_or("tool");
                let line = match state {
                    "ERROR" => {
                        let message = step
                            .pointer("/tool_info/error/message")
                            .and_then(Value::as_str)
                            .unwrap_or("failed");
                        format!("> {name} failed: {}\n", shorten(message))
                    }
                    _ => match tool_detail(step) {
                        Some(detail) => format!("> {name} {detail}\n"),
                        None => format!("> {name}\n"),
                    },
                };
                self.thinking(&line, out);
            }
            _ => {}
        }
    }

    fn on_result(&mut self, result: &Value, out: &mut Vec<u8>) {
        let count = |key: &str| {
            result
                .pointer(&format!("/usage/{key}"))
                .and_then(Value::as_u64)
                .unwrap_or_default()
        };
        self.usage = RunUsage {
            input_tokens: count("input_tokens"),
            cache_read_tokens: count("cache_read_tokens"),
            output_tokens: count("output_tokens"),
        };
        let status = result.get("status").and_then(Value::as_str).unwrap_or("");
        if self.emitted_text.is_empty() {
            let response = result.get("response").and_then(Value::as_str).unwrap_or("");
            if status != "SUCCESS" {
                let error = result
                    .get("error")
                    .and_then(Value::as_str)
                    .filter(|error| !error.is_empty())
                    .unwrap_or(status);
                self.text(&format!("agy error: {error}"), out);
            } else if !response.is_empty() {
                self.text(response, out);
            } else if let Some(denied) = denied_actions(result) {
                self.text(
                    &format!(
                        "agy produced no answer: it needed permission for {denied}, \
                         which agy: runs deny. Use agy-agent: for a run that may edit and \
                         run commands."
                    ),
                    out,
                );
            }
        }
        self.finish(out);
    }

    fn emit(out: &mut Vec<u8>, event: &str, data: &Value) {
        out.extend_from_slice(&encode_sse_event(Some(event), &data.to_string()));
    }

    fn ensure_message_started(&mut self, out: &mut Vec<u8>) {
        if self.message_started {
            return;
        }
        self.message_started = true;
        let data = json!({
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": null,
                "stop_sequence": null,
                "usage": { "input_tokens": 0, "output_tokens": 0 }
            }
        });
        Self::emit(out, "message_start", &data);
    }

    fn close_open_block(&mut self, out: &mut Vec<u8>) {
        match self.open {
            OpenBlock::None => {}
            OpenBlock::Thinking(index) => {
                let signature = ProxyReasoning::Agy.sign(&self.message_id, index);
                let data = json!({
                    "type": "content_block_delta",
                    "index": index,
                    "delta": { "type": "signature_delta", "signature": signature }
                });
                Self::emit(out, "content_block_delta", &data);
                Self::emit(
                    out,
                    "content_block_stop",
                    &json!({ "type": "content_block_stop", "index": index }),
                );
            }
            OpenBlock::Text(index) => Self::emit(
                out,
                "content_block_stop",
                &json!({ "type": "content_block_stop", "index": index }),
            ),
        }
        self.open = OpenBlock::None;
    }

    fn open_block(&mut self, kind: &str, out: &mut Vec<u8>) -> usize {
        self.close_open_block(out);
        let index = self.next_index;
        self.next_index += 1;
        let block = if kind == "thinking" {
            json!({ "type": "thinking", "thinking": "" })
        } else {
            json!({ "type": "text", "text": "" })
        };
        Self::emit(
            out,
            "content_block_start",
            &json!({ "type": "content_block_start", "index": index, "content_block": block }),
        );
        index
    }

    fn thinking(&mut self, text: &str, out: &mut Vec<u8>) {
        self.ensure_message_started(out);
        let index = match self.open {
            OpenBlock::Thinking(index) => index,
            _ => {
                let index = self.open_block("thinking", out);
                self.open = OpenBlock::Thinking(index);
                index
            }
        };
        let data = json!({
            "type": "content_block_delta",
            "index": index,
            "delta": { "type": "thinking_delta", "thinking": text }
        });
        Self::emit(out, "content_block_delta", &data);
    }

    fn text(&mut self, text: &str, out: &mut Vec<u8>) {
        self.ensure_message_started(out);
        let index = match self.open {
            OpenBlock::Text(index) => index,
            _ => {
                let index = self.open_block("text", out);
                self.open = OpenBlock::Text(index);
                index
            }
        };
        self.emitted_text.push_str(text);
        let data = json!({
            "type": "content_block_delta",
            "index": index,
            "delta": { "type": "text_delta", "text": text }
        });
        Self::emit(out, "content_block_delta", &data);
    }

    /// Emit an error as assistant text and terminate, so the client always
    /// gets a closed message.
    pub fn fail(&mut self, message: &str, out: &mut Vec<u8>) {
        if self.finished {
            return;
        }
        self.text(message, out);
        self.finish(out);
    }

    pub fn finish(&mut self, out: &mut Vec<u8>) {
        if self.finished {
            return;
        }
        self.finished = true;
        self.ensure_message_started(out);
        self.close_open_block(out);
        let data = json!({
            "type": "message_delta",
            "delta": { "stop_reason": "end_turn", "stop_sequence": null },
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "cache_read_input_tokens": self.usage.cache_read_tokens,
                "output_tokens": self.usage.output_tokens
            }
        });
        Self::emit(out, "message_delta", &data);
        Self::emit(out, "message_stop", &json!({ "type": "message_stop" }));
    }
}

impl AgentTranslator for EventTranslator {
    fn push_bytes(&mut self, chunk: &[u8]) -> Vec<u8> {
        EventTranslator::push_bytes(self, chunk)
    }
    fn fail(&mut self, message: &str, out: &mut Vec<u8>) {
        EventTranslator::fail(self, message, out)
    }
    fn is_finished(&self) -> bool {
        EventTranslator::is_finished(self)
    }
}

/// The first string parameter, e.g. a path or a command line.
fn tool_detail(step: &Value) -> Option<String> {
    let parameters = step.pointer("/tool_info/parameters")?.as_object()?;
    [
        "CommandLine",
        "Path",
        "AbsolutePath",
        "Query",
        "Url",
        "SearchPath",
    ]
    .iter()
    .find_map(|key| parameters.get(*key).and_then(Value::as_str))
    .or_else(|| parameters.values().find_map(Value::as_str))
    .map(shorten)
}

fn denied_actions(result: &Value) -> Option<String> {
    let names: Vec<&str> = result
        .get("denied_actions")?
        .as_array()?
        .iter()
        .filter_map(|action| {
            action
                .get("display_name")
                .or_else(|| action.get("action"))
                .and_then(Value::as_str)
        })
        .collect();
    (!names.is_empty()).then(|| names.join(", "))
}

fn shorten(value: &str) -> String {
    const LIMIT: usize = 120;
    let line = value.replace('\n', " ");
    if line.chars().count() <= LIMIT {
        return line;
    }
    format!("{}…", line.chars().take(LIMIT).collect::<String>())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn translate(input: &str) -> (Vec<u8>, EventTranslator) {
        let mut translator = EventTranslator::new("msg_1", "agy:test");
        let mut out = translator.push_bytes(input.as_bytes());
        translator.finish(&mut out);
        (out, translator)
    }

    fn events(sse: &[u8]) -> Vec<Value> {
        crate::anthropic::sse::parse_sse_events(sse)
            .into_iter()
            .map(|event| serde_json::from_str(&event.data).unwrap_or(Value::Null))
            .collect()
    }

    fn delta_text(sse: &[u8], pointer: &str) -> String {
        events(sse)
            .iter()
            .filter_map(|data| data.pointer(pointer).and_then(Value::as_str))
            .collect()
    }

    fn text_of(sse: &[u8]) -> String {
        delta_text(sse, "/delta/text")
    }

    fn thinking_of(sse: &[u8]) -> String {
        delta_text(sse, "/delta/thinking")
    }

    const READ_ONLY: &str =
        include_str!("../../../tests/fixtures/agy/read-only-denied-write.jsonl");
    const NO_ANSWER: &str = include_str!("../../../tests/fixtures/agy/plan-denied-no-answer.jsonl");
    const SIMPLE: &str = include_str!("../../../tests/fixtures/agy/stdin-simple.jsonl");
    const UNKNOWN_MODEL: &str = include_str!("../../../tests/fixtures/agy/unknown-model.jsonl");
    const SIGNED_OUT: &str = include_str!("../../../tests/fixtures/agy/signed-out.jsonl");

    /// The deltas are the answer exactly once; `result.response` repeats it and
    /// must not be appended.
    #[test]
    fn streamed_deltas_are_the_answer_once() {
        let (sse, translator) = translate(READ_ONLY);
        let expected = serde_json::from_str::<Value>(READ_ONLY.lines().last().unwrap()).unwrap()
            ["result"]["response"]
            .as_str()
            .unwrap()
            .to_string();
        assert_eq!(text_of(&sse), expected);
        assert_eq!(
            translator.usage(),
            &RunUsage {
                input_tokens: 18428,
                cache_read_tokens: 13564,
                output_tokens: 1497
            }
        );
    }

    /// agy's own tools are progress in the thinking channel, never `tool_use`.
    #[test]
    fn tools_become_signed_thinking_not_tool_use() {
        let (sse, _) = translate(READ_ONLY);
        let thinking = thinking_of(&sse);
        assert!(
            thinking.contains("> write_file /tmp/claude-1000/agy-cap/ws/created.txt\n"),
            "{thinking}"
        );
        assert!(thinking.contains("> write_file failed: permission check failed"));
        assert!(thinking.contains("> read_file /tmp/claude-1000/agy-cap/ws/a.txt\n"));
        let all = events(&sse);
        assert!(
            !all.iter()
                .any(|data| data.pointer("/content_block/type") == Some(&json!("tool_use")))
        );
        let signature = all
            .iter()
            .find_map(|data| data.pointer("/delta/signature").and_then(Value::as_str))
            .expect("thinking is signed");
        assert_eq!(ProxyReasoning::of(signature), Some(ProxyReasoning::Agy));
    }

    #[test]
    fn simple_run_streams_text_and_stops() {
        let (sse, _) = translate(SIMPLE);
        assert_eq!(text_of(&sse), "stdin ok\n");
        let all = events(&sse);
        assert_eq!(all.first().unwrap()["type"], "message_start");
        assert_eq!(all.last().unwrap()["type"], "message_stop");
    }

    #[test]
    fn a_denied_run_with_no_answer_says_why() {
        let (sse, _) = translate(NO_ANSWER);
        let text = text_of(&sse);
        assert!(text.contains("permission for Bash"), "{text}");
        assert!(text.contains("agy-agent:"), "{text}");
    }

    #[test]
    fn errors_reach_the_caller_as_text() {
        let (sse, _) = translate(UNKNOWN_MODEL);
        assert!(text_of(&sse).starts_with("agy error: invalid model selection"));
        let (sse, _) = translate(SIGNED_OUT);
        assert_eq!(
            text_of(&sse),
            "agy error: authentication failed or timed out"
        );
    }

    #[test]
    fn a_line_split_across_reads_is_reassembled() {
        let mut translator = EventTranslator::new("msg_1", "agy");
        let (head, tail) = SIMPLE.split_at(SIMPLE.len() / 2);
        let mut out = translator.push_bytes(head.as_bytes());
        out.extend(translator.push_bytes(tail.as_bytes()));
        assert!(translator.is_finished());
        assert_eq!(text_of(&out), "stdin ok\n");
    }

    #[test]
    fn a_later_answer_step_starts_a_new_paragraph() {
        let input = concat!(
            r#"{"event":"step_update","step_update":{"step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"Looking."}}"#,
            "\n",
            r#"{"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"list_dir","tool_info":{"name":"list_dir","parameters":{"DirectoryPath":"/w"}}}}"#,
            "\n",
            r#"{"event":"step_update","step_update":{"step_index":3,"state":"ACTIVE","step_type":"agent_response","text_delta":"Done."}}"#,
            "\n",
            r#"{"event":"result","result":{"status":"SUCCESS","response":"Looking.Done.","usage":{}}}"#,
            "\n"
        );
        let (sse, _) = translate(input);
        assert_eq!(text_of(&sse), "Looking.\n\nDone.");
        assert_eq!(thinking_of(&sse), "> list_dir /w\n");
    }
}
